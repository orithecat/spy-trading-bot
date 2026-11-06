import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from trading_hours import (
    can_open_new_position,
    get_entry_block_reason,
    get_trading_status,
)


PACIFIC = ZoneInfo("America/Los_Angeles")


def pacific_time(hour, minute, second=0):
    return datetime(2026, 11, 4, hour, minute, second, tzinfo=PACIFIC)


class TestTradingHours(unittest.TestCase):

    def test_before_market_open_blocks_all_new_entries(self):
        now = pacific_time(6, 29, 59)

        self.assertFalse(can_open_new_position("0DTE", now))
        self.assertFalse(can_open_new_position("1DTE", now))

    def test_market_open_allows_both_expiries(self):
        now = pacific_time(6, 30)

        self.assertTrue(can_open_new_position("0DTE", now))
        self.assertTrue(can_open_new_position("1DTE", now))

    def test_before_eleven_allows_both_expiries(self):
        now = pacific_time(10, 59, 59)

        self.assertTrue(can_open_new_position("0DTE", now))
        self.assertTrue(can_open_new_position("1DTE", now))

    def test_eleven_blocks_only_new_zero_dte_entries(self):
        now = pacific_time(11, 0)

        self.assertFalse(can_open_new_position("0DTE", now))
        self.assertTrue(can_open_new_position("1DTE", now))

        self.assertIn(
            "11:00 AM",
            get_entry_block_reason("0DTE", now),
        )

    def test_before_close_allows_only_one_dte(self):
        now = pacific_time(12, 59, 59)

        self.assertFalse(can_open_new_position("0DTE", now))
        self.assertTrue(can_open_new_position("1DTE", now))

    def test_close_blocks_all_new_entries(self):
        now = pacific_time(13, 0)

        self.assertFalse(can_open_new_position("0DTE", now))
        self.assertFalse(can_open_new_position("1DTE", now))

        self.assertIn(
            "1:00 PM",
            get_entry_block_reason("1DTE", now),
        )

    def test_existing_position_management_is_never_blocked_by_time_gate(self):
        for hour, minute in ((6, 0), (11, 0), (13, 0), (16, 0)):
            status = get_trading_status(pacific_time(hour, minute))
            self.assertTrue(status["manage_existing_positions"])

    def test_unsupported_expiry_is_rejected(self):
        now = pacific_time(10, 0)

        self.assertFalse(can_open_new_position("2DTE", now))
        self.assertEqual(
            get_entry_block_reason("2DTE", now),
            "Unsupported option expiry.",
        )


if __name__ == "__main__":
    unittest.main()
