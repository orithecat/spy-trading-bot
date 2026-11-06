import logging
import math
import os
import time

from datetime import datetime, timedelta, timezone, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from option_contracts import OptionContractFinder
from option_history import OptionHistory

from webull.core.client import ApiClient
from webull.core.http.initializer.token.token_manager import TokenManager
from webull.data.quotes.market_data import MarketData
from webull.data.common.category import Category
from webull.data.common.timespan import Timespan
from webull.data.request.get_option_snapshot_request import (
    GetOptionSnapshotRequest,
)


class MarketDataEngine:
    """
    Read-only Webull market data engine.

    Retrieves and validates market data only.
    Does not place, modify, or cancel orders.
    """

    MARKET_TIMEZONE = ZoneInfo("America/New_York")

    M1_MAX_AGE_SECONDS = 120
    M5_MAX_AGE_SECONDS = 600
    OPTION_MAX_AGE_SECONDS = 10

    def __init__(self):
        load_dotenv(Path(__file__).resolve().parent / ".env")

        app_key = os.getenv("WEBULL_APP_KEY")
        app_secret = os.getenv("WEBULL_APP_SECRET")

        if not app_key or not app_secret:
            raise RuntimeError("Missing Webull API credentials in .env")

        self.client = ApiClient(
            app_key,
            app_secret,
            "us",
        )

        TokenManager().init_token(self.client)

        self.stocks = MarketData(self.client)
        self.option_contract_finder = OptionContractFinder(self.client)
        self.option_history = OptionHistory(self.client)

    @staticmethod
    def _to_float(value):
        try:
            if value is None:
                return None

            result = float(value)

            if not math.isfinite(result):
                return None

            return result

        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_timestamp(value):
        if not value:
            return None

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return parsed.astimezone(timezone.utc)

        except (TypeError, ValueError, AttributeError):
            return None

    @staticmethod
    def _is_rth_bar(bar):
        return str(bar.get("trading_session", "")).upper() == "RTH"

    @classmethod
    def is_regular_trading_session(cls, now=None):
        """
        Check US regular trading hours.

        Monday-Friday:
        09:30-16:00 America/New_York.

        Holiday validation is additionally protected by
        the market-data freshness checks.
        """

        now = now or datetime.now(timezone.utc)

        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        local_time = now.astimezone(cls.MARKET_TIMEZONE)

        if local_time.weekday() >= 5:
            return False

        current_time = local_time.time()

        return dt_time(9, 30) <= current_time < dt_time(16, 0)

    @classmethod
    def parse_bars(cls, response):
        """
        Parse Webull batch history response.
        Return chronological bars.
        """

        # Webull SDK may return a requests.Response instead of decoded JSON.
        if hasattr(response, "status_code") and callable(
            getattr(response, "json", None)
        ):
            response.raise_for_status()
            response = response.json()

        if not isinstance(response, dict):
            raise ValueError("Unexpected history response format")

        results = response.get("result")

        if not isinstance(results, list) or not results:
            raise ValueError("No history data returned")

        spy_result = next(
            (
                item.get("result")
                for item in results
                if (isinstance(item, dict) and item.get("symbol") == "SPY")
            ),
            None,
        )

        if not isinstance(spy_result, list) or not spy_result:
            raise ValueError("SPY history data is missing")

        bars = []
        seen_timestamps = set()

        for item in spy_result:

            if not isinstance(item, dict):
                continue

            timestamp = cls._parse_timestamp(item.get("time"))

            close = cls._to_float(item.get("close"))

            if timestamp is None or close is None:
                continue

            if close <= 0:
                continue

            # Duplicate timestamps are not accepted.
            if timestamp in seen_timestamps:
                continue

            seen_timestamps.add(timestamp)

            bars.append(
                {
                    "time": timestamp,
                    "open": cls._to_float(item.get("open")),
                    "high": cls._to_float(item.get("high")),
                    "low": cls._to_float(item.get("low")),
                    "close": close,
                    "volume": cls._to_float(item.get("volume")),
                    "trading_session": item.get("trading_session"),
                }
            )

        bars.sort(key=lambda bar: bar["time"])

        if len(bars) < 50:
            raise ValueError(f"Insufficient valid SPY bars: {len(bars)}")

        return bars

    @staticmethod
    def calculate_ema(values, period):

        if period <= 0:
            raise ValueError("EMA period must be positive")

        if len(values) < period:
            return None

        multiplier = 2 / (period + 1)

        ema = sum(values[:period]) / period

        for value in values[period:]:
            ema = (value - ema) * multiplier + ema

        return ema

    @staticmethod
    def calculate_ema_series(values, period):
        """
        Calculate the complete EMA series.

        The first valid EMA uses the SMA seed.
        Earlier positions contain None.
        """

        if period <= 0:
            raise ValueError("EMA period must be positive")

        result = [None] * len(values)

        if len(values) < period:
            return result

        multiplier = 2 / (period + 1)

        ema = sum(values[:period]) / period

        result[period - 1] = ema

        for index in range(period, len(values)):

            ema = (values[index] - ema) * multiplier + ema

            result[index] = ema

        return result

    @classmethod
    def calculate_indicators(cls, bars, now=None):

        now = now or datetime.now(timezone.utc)

        if not isinstance(bars, list) or len(bars) < 56:
            raise ValueError("At least 56 bars are required")

        # Only completed regular-session bars.
        completed_bars = [
            bar
            for bar in bars
            if (
                isinstance(bar.get("time"), datetime)
                and cls._is_rth_bar(bar)
                and bar["time"] + timedelta(minutes=1) <= now
            )
        ]

        if len(completed_bars) < 55:
            raise ValueError("Insufficient completed one-minute RTH bars")

        closes = [bar["close"] for bar in completed_bars]

        ema9_series = cls.calculate_ema_series(closes, 9)

        ema21_series = cls.calculate_ema_series(closes, 21)

        ema50_series = cls.calculate_ema_series(closes, 50)

        ema9 = ema9_series[-1]
        ema21 = ema21_series[-1]
        ema50 = ema50_series[-1]

        # Five completed one-minute bars earlier.
        # Use the same EMA series to preserve its seed.
        ema21_5m_ago = ema21_series[-6]
        ema50_5m_ago = ema50_series[-6]

        latest_bar_time = completed_bars[-1]["time"]

        latest_bar_close_time = latest_bar_time + timedelta(minutes=1)

        bar_age_seconds = max(
            0.0,
            (now - latest_bar_close_time).total_seconds(),
        )

        return {
            "spy_price": closes[-1],
            "ema9": ema9,
            "ema21": ema21,
            "ema50": ema50,
            "ema_gap": ema21 - ema50,
            "ema_gap_5m_ago": (ema21_5m_ago - ema50_5m_ago),
            "latest_bar_time": latest_bar_time,
            "latest_bar_close_time": latest_bar_close_time,
            "bar_age_seconds": bar_age_seconds,
            "completed_bar_count": len(completed_bars),
        }

    @classmethod
    def calculate_five_min_trend(
        cls,
        bars,
        now=None,
    ):

        now = now or datetime.now(timezone.utc)

        completed_bars = [
            bar
            for bar in bars
            if (cls._is_rth_bar(bar) and bar["time"] + timedelta(minutes=5) <= now)
        ]

        if len(completed_bars) < 55:
            return "NEUTRAL"

        closes = [bar["close"] for bar in completed_bars]

        ema21 = cls.calculate_ema(closes, 21)

        ema50 = cls.calculate_ema(closes, 50)

        latest_price = closes[-1]

        if ema21 > ema50 and latest_price > ema21:
            return "UP"

        if ema21 < ema50 and latest_price < ema21:
            return "DOWN"

        return "NEUTRAL"

    def get_spy_bars(
        self,
        timespan,
        count="200",
    ):

        response = self.stocks.get_batch_history_bar(
            symbols=["SPY"],
            category=Category.US_STOCK.name,
            timespan=timespan,
            count=count,
        )

        return self.parse_bars(response)

    def get_option_snapshot(
        self,
        option_symbol,
        max_age_seconds=None,
    ):

        if not option_symbol:
            raise ValueError("An option symbol is required")

        if max_age_seconds is None:
            max_age_seconds = self.OPTION_MAX_AGE_SECONDS

        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")

        request = GetOptionSnapshotRequest()

        request.set_symbols(option_symbol)
        request.set_category("US_OPTION")

        response = self.client.get_response(request)

        if not isinstance(response, list) or not response:
            raise ValueError("No option quote returned")

        quote = response[0]

        if not isinstance(quote, dict):
            raise ValueError("Unexpected option quote format")

        returned_symbol = quote.get("symbol")

        if returned_symbol != option_symbol:
            raise ValueError("Option quote symbol mismatch")

        bid = self._to_float(quote.get("bid"))

        ask = self._to_float(quote.get("ask"))

        quote_time = self._to_float(quote.get("quote_time"))

        delay_minutes = self._to_float(quote.get("delay_minutes"))

        if bid is None or ask is None or bid <= 0 or ask <= 0:
            raise ValueError("Invalid option bid/ask")

        if ask < bid:
            raise ValueError("Option ask is below bid")

        if quote_time is None or quote_time <= 0:
            raise ValueError("Option quote timestamp is missing or invalid")

        if delay_minutes is None or delay_minutes != 0:
            raise ValueError("Delayed option quotes are not allowed")

        bid_size = self._to_float(quote.get("bid_size"))

        ask_size = self._to_float(quote.get("ask_size"))

        if bid_size is None or ask_size is None or bid_size <= 0 or ask_size <= 0:
            raise ValueError("Invalid option bid/ask size")

        quote_age_seconds = (time.time() * 1000 - quote_time) / 1000

        if quote_age_seconds < -2:
            raise ValueError("Option quote timestamp is in the future")

        if quote_age_seconds > max_age_seconds:
            raise ValueError(f"Option quote is stale: {quote_age_seconds:.1f}s")

        quote_age_seconds = max(
            0.0,
            quote_age_seconds,
        )

        midpoint = (bid + ask) / 2

        return {
            "symbol": returned_symbol,
            "bid": bid,
            "ask": ask,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "midpoint": midpoint,
            "spread_pct": ((ask - bid) / midpoint * 100),
            "quote_age_seconds": quote_age_seconds,
            "delay_minutes": delay_minutes,
            "quote_valid": True,
        }

    def get_market_snapshot(self):

        now = datetime.now(timezone.utc)

        one_minute_bars = self.get_spy_bars(
            Timespan.M1.name,
            count="200",
        )

        five_minute_bars = self.get_spy_bars(
            Timespan.M5.name,
            count="200",
        )

        indicators = self.calculate_indicators(
            one_minute_bars,
            now=now,
        )

        five_min_completed = [
            bar
            for bar in five_minute_bars
            if (self._is_rth_bar(bar) and bar["time"] + timedelta(minutes=5) <= now)
        ]

        if len(five_min_completed) < 55:
            raise ValueError("Insufficient completed five-minute RTH bars")

        five_min_latest_time = five_min_completed[-1]["time"]

        five_min_close_time = five_min_latest_time + timedelta(minutes=5)

        five_min_age_seconds = max(
            0.0,
            (now - five_min_close_time).total_seconds(),
        )

        five_min_trend = self.calculate_five_min_trend(
            five_minute_bars,
            now=now,
        )

        indicators["five_min_trend"] = five_min_trend

        indicators["five_min_latest_bar_time"] = five_min_latest_time

        indicators["five_min_close_time"] = five_min_close_time

        indicators["five_min_bar_age_seconds"] = five_min_age_seconds

        rth_active = self.is_regular_trading_session(now)

        indicators["regular_trading_session"] = rth_active

        indicators["entry_data_valid"] = (
            rth_active
            and indicators["bar_age_seconds"] <= self.M1_MAX_AGE_SECONDS
            and five_min_age_seconds <= self.M5_MAX_AGE_SECONDS
            and five_min_trend in ("UP", "DOWN")
        )

        if not rth_active:
            indicators["signal_status"] = "Blocked: outside regular trading hours"

        elif indicators["bar_age_seconds"] > self.M1_MAX_AGE_SECONDS:
            indicators["signal_status"] = "Blocked: stale one-minute data"

        elif five_min_age_seconds > self.M5_MAX_AGE_SECONDS:
            indicators["signal_status"] = "Blocked: stale five-minute data"

        elif five_min_trend not in ("UP", "DOWN"):
            indicators["signal_status"] = "Blocked: unconfirmed five-minute trend"

        else:
            indicators["signal_status"] = "Market data valid"

        return indicators


    def get_nearest_otm_option_snapshot(
        self,
        expiry,
        option_type,
        spy_price,
        max_age_seconds=None,
    ):
        """Select the nearest OTM SPY contract and retrieve its validated quote.

        This method is read-only. It does not submit, replace, or cancel orders.
        """

        contract = self.option_contract_finder.find_nearest_otm(
            expiry=expiry,
            option_type=option_type,
            underlying_price=spy_price,
        )

        quote = self.get_option_snapshot(
            contract["symbol"],
            max_age_seconds=max_age_seconds,
        )

        return {
            "contract": contract,
            "quote": quote,
        }

    def get_s1_option_market_data(
        self,
        expiry,
        option_type,
        lookback_minutes=30,
        max_quote_age_seconds=None,
        max_bar_age_seconds=120,
    ):
        """Retrieve validated SPY trend, OTM option quote, and recent option low.

        Read-only. Any invalid or stale component raises an exception.
        """

        market = self.get_market_snapshot()

        if not market.get("entry_data_valid"):
            raise ValueError(
                "SPY market data is not valid for a new entry: "
                + str(market.get("signal_status", "unknown"))
            )

        option_data = self.get_nearest_otm_option_snapshot(
            expiry=expiry,
            option_type=option_type,
            spy_price=market["spy_price"],
            max_age_seconds=max_quote_age_seconds,
        )

        history = self.option_history.get_recent_low(
            option_symbol=option_data["contract"]["symbol"],
            lookback_minutes=lookback_minutes,
            max_bar_age_seconds=max_bar_age_seconds,
        )

        if not history.get("history_valid"):
            raise ValueError("Option history is not valid")

        return {
            "market": market,
            "contract": option_data["contract"],
            "quote": option_data["quote"],
            "option_history": history,
            "option_low": history["recent_low"],
            "entry_data_valid": True,
        }
