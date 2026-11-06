import unittest
from datetime import datetime, timezone

from option_history import OptionHistory


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeMarketData:
    def __init__(self, payload):
        self.payload = payload

    def get_option_history_bars(self, **kwargs):
        return FakeResponse(self.payload)


class TestOptionHistory(unittest.TestCase):

    SYMBOL = "SPY261105C00761000"

    def make_history(self, bars, delay_minutes=0):
        history = OptionHistory.__new__(OptionHistory)
        history.market_data = FakeMarketData([
            {
                "symbol": self.SYMBOL,
                "delay_minutes": delay_minutes,
                "result": bars,
            }
        ])
        return history

    def make_bar(self, time, low, volume="10"):
        return {
            "time": time,
            "low": str(low),
            "volume": str(volume),
        }

    def test_returns_lowest_completed_bar_in_lookback(self):
        history = self.make_history([
            self.make_bar("2026-11-05T21:14:00.000+0000", 1.20),
            self.make_bar("2026-11-05T21:13:00.000+0000", 1.10),
            self.make_bar("2026-11-05T21:05:00.000+0000", 1.30),
        ])

        result = history.get_recent_low(
            self.SYMBOL,
            now=datetime(2026, 11, 5, 21, 15, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result["recent_low"], 1.10)
        self.assertEqual(result["bar_count"], 3)
        self.assertTrue(result["history_valid"])

    def test_excludes_incomplete_current_minute(self):
        history = self.make_history([
            self.make_bar("2026-11-05T21:15:00.000+0000", 0.50),
            self.make_bar("2026-11-05T21:14:00.000+0000", 1.20),
        ])

        result = history.get_recent_low(
            self.SYMBOL,
            now=datetime(2026, 11, 5, 21, 15, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result["recent_low"], 1.20)
        self.assertEqual(result["bar_count"], 1)

    def test_excludes_bars_outside_lookback(self):
        history = self.make_history([
            self.make_bar("2026-11-05T20:40:00.000+0000", 0.50),
            self.make_bar("2026-11-05T21:14:00.000+0000", 1.20),
        ])

        result = history.get_recent_low(
            self.SYMBOL,
            lookback_minutes=30,
            now=datetime(2026, 11, 5, 21, 15, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result["recent_low"], 1.20)
        self.assertEqual(result["bar_count"], 1)

    def test_rejects_stale_history(self):
        history = self.make_history([
            self.make_bar("2026-11-05T21:11:00.000+0000", 1.20),
        ])

        with self.assertRaises(ValueError):
            history.get_recent_low(
                self.SYMBOL,
                max_bar_age_seconds=120,
                now=datetime(2026, 11, 5, 21, 15, 30, tzinfo=timezone.utc),
            )

    def test_rejects_delayed_history(self):
        history = self.make_history([
            self.make_bar("2026-11-05T21:14:00.000+0000", 1.20),
        ], delay_minutes=1)

        with self.assertRaises(ValueError):
            history.get_recent_low(
                self.SYMBOL,
                now=datetime(2026, 11, 5, 21, 15, 30, tzinfo=timezone.utc),
            )

    def test_skips_invalid_low_and_zero_volume(self):
        history = self.make_history([
            self.make_bar("2026-11-05T21:14:00.000+0000", 0, 10),
            self.make_bar("2026-11-05T21:13:00.000+0000", 0.50, 0),
            self.make_bar("2026-11-05T21:13:00.000+0000", 1.25, 10),
        ])

        result = history.get_recent_low(
            self.SYMBOL,
            now=datetime(2026, 11, 5, 21, 15, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result["recent_low"], 1.25)
        self.assertEqual(result["bar_count"], 1)

    def test_rejects_missing_symbol(self):
        history = self.make_history([
            self.make_bar("2026-11-05T21:14:00.000+0000", 1.20),
        ])

        with self.assertRaises(ValueError):
            history.get_recent_low(
                "SPY_WRONG_SYMBOL",
                now=datetime(2026, 11, 5, 21, 15, 30, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
