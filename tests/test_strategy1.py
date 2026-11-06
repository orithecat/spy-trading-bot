import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from strategies.strategy1 import evaluate_entry, validate_settings
import trade_logger
from risk import check_daily_equity


def passing_call(**overrides):
    values = {
        "direction": "CALL",
        "expiry": "0DTE",
        "now": datetime(2026, 11, 4, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
        "ema9": 101.2,
        "ema21": 101,
        "ema50": 100,
        "ema_gap_now": 1.2,
        "ema_gap_5m_ago": 1.0,
        "spy_price": 101.5,
        "five_min_trend": "UP",
        "option_low": 2.0,
        "option_ask": 2.07,
        "bid": 2.06,
        "ask": 2.07,
        "quote_age_seconds": 2,
        "rebound_trigger_pct": 3,
    }
    values.update(overrides)
    return values


class Strategy1Tests(unittest.TestCase):
    def test_defaults_and_rebound_validation(self):
        settings = validate_settings({
            "expiry": "0DTE",
            "max_trade_amount": 500,
            "take_profit_pct": 5,
            "stop_loss_pct": 5,
            "rebound_trigger_pct": 3,
        })
        self.assertEqual(settings["expiry"], "0DTE")
        self.assertEqual(settings["rebound_trigger_pct"], 3)
        self.assertEqual(
            settings["entry_filters"],
            {"ema_gap_not_shrinking": True},
        )

    def test_standard_entry_passes_when_all_checks_pass(self):
        decision = evaluate_entry(**passing_call())
        self.assertTrue(decision["eligible"], decision["reasons"])

    def test_rejects_wide_spread(self):
        decision = evaluate_entry(**passing_call(bid=1.90))
        self.assertFalse(decision["eligible"])
        self.assertTrue(
            any("spread" in reason.lower() for reason in decision["reasons"])
        )

    def test_legacy_settings_cannot_disable_mandatory_filters(self):
        settings = validate_settings({
            "expiry": "0DTE",
            "max_trade_amount": 500,
            "take_profit_pct": 5,
            "stop_loss_pct": 5,
            "rebound_trigger_pct": 3,
            "entry_filters": {
                "ema_alignment": False,
                "ema_gap_positive": False,
                "five_min_trend_confirmation": False,
                "option_rebound": False,
                "quote_quality": False,
                "ema_gap_not_shrinking": False,
            },
        })
        self.assertEqual(
            settings["entry_filters"],
            {"ema_gap_not_shrinking": False},
        )

    def test_mandatory_filters_cannot_be_disabled_during_evaluation(self):
        decision = evaluate_entry(
            **passing_call(
                ema9=99,
                ema21=100,
                ema50=101,
                spy_price=99.5,
                five_min_trend="DOWN",
            ),
            entry_filters={
                "ema_alignment": False,
                "five_min_trend_confirmation": False,
                "option_rebound": False,
                "quote_quality": False,
                "ema_gap_not_shrinking": False,
            },
        )
        self.assertFalse(decision["eligible"])
        self.assertEqual(
            decision["filter_results"]["ema_alignment"]["status"],
            "fail",
        )
        self.assertEqual(
            decision["filter_results"]["five_min_trend_confirmation"]["status"],
            "fail",
        )

    def test_optional_gap_filter_can_be_disabled(self):
        decision = evaluate_entry(
            **passing_call(ema_gap_now=0.8, ema_gap_5m_ago=1.2),
            entry_filters={"ema_gap_not_shrinking": False},
        )
        self.assertTrue(decision["eligible"], decision["reasons"])
        self.assertEqual(
            decision["filter_results"]["ema_gap_not_shrinking"]["status"],
            "disabled",
        )

    def test_put_gap_uses_absolute_separation(self):
        decision = evaluate_entry(
            direction="PUT",
            expiry="0DTE",
            now=datetime(2026, 11, 4, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
            ema9=99,
            ema21=100,
            ema50=101,
            ema_gap_now=-1.2,
            ema_gap_5m_ago=-1.0,
            spy_price=99.5,
            five_min_trend="DOWN",
            option_low=2.0,
            option_ask=2.07,
            bid=2.06,
            ask=2.07,
            quote_age_seconds=2,
            rebound_trigger_pct=3,
        )
        self.assertTrue(decision["eligible"], decision["reasons"])

    def test_invalid_quote_is_always_blocked(self):
        decision = evaluate_entry(
            **passing_call(bid=2.10, ask=2.07),
            entry_filters={"quote_quality": False},
        )
        self.assertFalse(decision["eligible"])
        self.assertIn("Invalid option quote", decision["reasons"][0])

    def test_daily_peak_drawdown_latches_and_settings_do_not_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "risk.json"
            settings = {
                "daily_profit_cap_pct": 30,
                "peak_drawdown_limit_pct": 10,
            }
            now = datetime(
                2026, 10, 28, 10, 0,
                tzinfo=ZoneInfo("America/New_York"),
            )
            check_daily_equity(10000, settings, path, now)
            below_cap = check_daily_equity(12900, settings, path, now)
            self.assertFalse(below_cap["latched"])
            latched = check_daily_equity(11610, settings, path, now)
            self.assertTrue(latched["latched"])
            settings["peak_drawdown_limit_pct"] = 50
            still_latched = check_daily_equity(12000, settings, path, now)
            self.assertTrue(still_latched["latched"])

    def test_entry_decision_is_logged_to_date_file(self):
        with tempfile.TemporaryDirectory() as directory:
            original = trade_logger.LOG_DIR
            try:
                trade_logger.LOG_DIR = Path(directory)
                decision = evaluate_entry(**passing_call())
                files = list(Path(directory).glob("*.jsonl"))
                self.assertEqual(len(files), 1)
                row = json.loads(files[0].read_text().splitlines()[0])
                self.assertEqual(row["event"], "entry_decision")
                self.assertTrue(row["decision"]["eligible"])
                self.assertIn("filter_results", row)
                self.assertIn("active_entry_filters", row)
            finally:
                trade_logger.LOG_DIR = original

    def test_logger_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            original = trade_logger.LOG_DIR
            try:
                trade_logger.LOG_DIR = Path(directory)
                trade_logger.log_event(
                    "test",
                    api_secret="do-not-store",
                    access_token="hidden",
                    value=7,
                )
                path = next(Path(directory).glob("*.jsonl"))
                row = json.loads(path.read_text().splitlines()[0])
                self.assertEqual(row["api_secret"], "[REDACTED]")
                self.assertEqual(row["access_token"], "[REDACTED]")
                self.assertEqual(row["value"], 7)
            finally:
                trade_logger.LOG_DIR = original


if __name__ == "__main__":
    unittest.main()
