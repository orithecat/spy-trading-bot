import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from market_data import MarketDataEngine


class TestMarketData(unittest.TestCase):

    def make_response(self, count=60):
        start = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)
        bars = []

        for i in range(count):
            price = 700 + i

            bars.append(
                {
                    "time": (start + timedelta(minutes=i)).isoformat(),
                    "open": str(price),
                    "high": str(price + 1),
                    "low": str(price - 1),
                    "close": str(price),
                    "volume": "1000",
                    "trading_session": "RTH",
                }
            )

        # Simulate Webull's newest-first response.
        bars.reverse()

        return {
            "result": [
                {
                    "symbol": "SPY",
                    "result": bars,
                }
            ]
        }

    def make_option_engine(self, quote):
        class FakeClient:
            def get_response(self, request):
                return [quote]

        engine = MarketDataEngine.__new__(MarketDataEngine)
        engine.client = FakeClient()
        return engine

    def make_option_quote(self, quote_time=998000, delay_minutes="0"):
        return {
            "symbol": "SPY261218C00640000",
            "bid": "2.00",
            "ask": "2.10",
            "bid_size": "10",
            "ask_size": "12",
            "quote_time": quote_time,
            "delay_minutes": delay_minutes,
        }

    def test_bars_are_sorted_chronologically(self):
        bars = MarketDataEngine.parse_bars(self.make_response())

        self.assertEqual(len(bars), 60)
        self.assertLess(bars[0]["time"], bars[-1]["time"])
        self.assertEqual(bars[0]["close"], 700.0)
        self.assertEqual(bars[-1]["close"], 759.0)

    def test_parse_bars_accepts_requests_response(self):
        import json
        from unittest.mock import Mock

        response = Mock()
        response.status_code = 200
        response.json.return_value = self.make_response()
        response.raise_for_status.return_value = None

        bars = MarketDataEngine.parse_bars(response)

        response.raise_for_status.assert_called_once()
        response.json.assert_called_once()
        self.assertEqual(len(bars), 60)
        self.assertEqual(bars[0]["close"], 700.0)

    def test_parse_bars_rejects_http_error_response(self):
        from unittest.mock import Mock

        response = Mock()
        response.status_code = 500
        response.raise_for_status.side_effect = RuntimeError("HTTP 500")

        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            MarketDataEngine.parse_bars(response)

        response.json.assert_not_called()

    def test_ema_calculation(self):
        values = list(range(1, 11))

        ema = MarketDataEngine.calculate_ema(values, 3)

        self.assertAlmostEqual(ema, 9.0)

    def test_ema_returns_none_when_insufficient_data(self):
        self.assertIsNone(MarketDataEngine.calculate_ema([1, 2], 3))

    def test_market_indicators(self):
        bars = MarketDataEngine.parse_bars(self.make_response())

        result = MarketDataEngine.calculate_indicators(bars)

        self.assertEqual(result["spy_price"], 759.0)
        self.assertIsNotNone(result["ema9"])
        self.assertIsNotNone(result["ema21"])
        self.assertIsNotNone(result["ema50"])
        self.assertIsNotNone(result["ema_gap"])
        self.assertIsNotNone(result["ema_gap_5m_ago"])
        self.assertGreaterEqual(result["bar_age_seconds"], 0)

    def test_five_minute_trend(self):
        bars = MarketDataEngine.parse_bars(self.make_response())

        trend = MarketDataEngine.calculate_five_min_trend(bars)

        self.assertEqual(trend, "UP")

    def test_insufficient_bars_raise_error(self):
        with self.assertRaises(ValueError):
            MarketDataEngine.parse_bars(self.make_response(20))

    def test_valid_option_quote_passes(self):
        engine = self.make_option_engine(self.make_option_quote())

        with patch("market_data.time.time", return_value=1000):
            result = engine.get_option_snapshot("SPY261218C00640000")

        self.assertTrue(result["quote_valid"])
        self.assertAlmostEqual(result["spread_pct"], 4.87804878)
        self.assertAlmostEqual(result["quote_age_seconds"], 2)

    def test_stale_option_quote_is_rejected(self):
        quote = self.make_option_quote(quote_time=989000)
        engine = self.make_option_engine(quote)

        with patch("market_data.time.time", return_value=1000):
            with self.assertRaisesRegex(ValueError, "stale"):
                engine.get_option_snapshot("SPY261218C00640000")

    def test_delayed_option_quote_is_rejected(self):
        quote = self.make_option_quote(delay_minutes="1")
        engine = self.make_option_engine(quote)

        with patch("market_data.time.time", return_value=1000):
            with self.assertRaisesRegex(ValueError, "Delayed"):
                engine.get_option_snapshot("SPY261218C00640000")

    def test_missing_option_timestamp_is_rejected(self):
        quote = self.make_option_quote(quote_time=None)
        engine = self.make_option_engine(quote)

        with patch("market_data.time.time", return_value=1000):
            with self.assertRaisesRegex(ValueError, "timestamp"):
                engine.get_option_snapshot("SPY261218C00640000")

    def test_future_option_timestamp_is_rejected(self):
        quote = self.make_option_quote(quote_time=1005000)
        engine = self.make_option_engine(quote)

        with patch("market_data.time.time", return_value=1000):
            with self.assertRaisesRegex(ValueError, "future"):
                engine.get_option_snapshot("SPY261218C00640000")


if __name__ == "__main__":
    unittest.main()


class TestNearestOtmOptionSnapshot(unittest.TestCase):

    def test_selects_contract_and_returns_validated_quote(self):
        engine = MarketDataEngine.__new__(MarketDataEngine)

        expected_contract = {
            "symbol": "SPY261105C00761000",
            "option_type": "CALL",
            "strike_price": "761.0000000000",
        }

        expected_quote = {
            "symbol": "SPY261105C00761000",
            "bid": 2.00,
            "ask": 2.10,
            "quote_valid": True,
        }

        class FakeFinder:
            def find_nearest_otm(self, **kwargs):
                self.kwargs = kwargs
                return expected_contract

        engine.option_contract_finder = FakeFinder()
        engine.get_option_snapshot = unittest.mock.Mock(
            return_value=expected_quote
        )

        result = engine.get_nearest_otm_option_snapshot(
            expiry="2026-11-05",
            option_type="CALL",
            spy_price=760.3,
        )

        self.assertEqual(result["contract"], expected_contract)
        self.assertEqual(result["quote"], expected_quote)
        engine.get_option_snapshot.assert_called_once_with(
            "SPY261105C00761000",
            max_age_seconds=None,
        )
        self.assertEqual(
            engine.option_contract_finder.kwargs["underlying_price"],
            760.3,
        )

    def test_invalid_quote_error_is_propagated(self):
        engine = MarketDataEngine.__new__(MarketDataEngine)

        class FakeFinder:
            def find_nearest_otm(self, **kwargs):
                return {
                    "symbol": "SPY261105P00760000",
                    "option_type": "PUT",
                    "strike_price": "760.0000000000",
                }

        engine.option_contract_finder = FakeFinder()

        def reject_quote(*args, **kwargs):
            raise ValueError("Option quote is stale")

        engine.get_option_snapshot = reject_quote

        with self.assertRaisesRegex(ValueError, "stale"):
            engine.get_nearest_otm_option_snapshot(
                expiry="2026-11-05",
                option_type="PUT",
                spy_price=760.3,
            )


class TestS1OptionMarketData(unittest.TestCase):

    def make_engine(self):
        engine = MarketDataEngine.__new__(MarketDataEngine)

        engine.get_market_snapshot = unittest.mock.Mock(
            return_value={
                "spy_price": 760.3,
                "ema9": 761.0,
                "ema21": 760.8,
                "ema50": 760.0,
                "ema_gap": 0.8,
                "ema_gap_5m_ago": 0.5,
                "five_min_trend": "UP",
                "entry_data_valid": True,
                "signal_status": "Market data valid",
            }
        )

        engine.get_nearest_otm_option_snapshot = unittest.mock.Mock(
            return_value={
                "contract": {
                    "symbol": "SPY261105C00761000",
                    "option_type": "CALL",
                    "strike_price": "761",
                },
                "quote": {
                    "symbol": "SPY261105C00761000",
                    "bid": 2.0,
                    "ask": 2.1,
                    "quote_valid": True,
                },
            }
        )

        engine.option_history = unittest.mock.Mock()
        engine.option_history.get_recent_low.return_value = {
            "symbol": "SPY261105C00761000",
            "recent_low": 1.85,
            "lookback_minutes": 30,
            "bar_count": 12,
            "history_valid": True,
        }

        return engine

    def test_returns_complete_validated_s1_market_data(self):
        engine = self.make_engine()

        result = engine.get_s1_option_market_data(
            expiry="2026-11-05",
            option_type="CALL",
        )

        self.assertTrue(result["entry_data_valid"])
        self.assertEqual(result["market"]["spy_price"], 760.3)
        self.assertEqual(result["contract"]["symbol"], "SPY261105C00761000")
        self.assertEqual(result["quote"]["ask"], 2.1)
        self.assertEqual(result["option_low"], 1.85)

        engine.get_nearest_otm_option_snapshot.assert_called_once_with(
            expiry="2026-11-05",
            option_type="CALL",
            spy_price=760.3,
            max_age_seconds=None,
        )

        engine.option_history.get_recent_low.assert_called_once_with(
            option_symbol="SPY261105C00761000",
            lookback_minutes=30,
            max_bar_age_seconds=120,
        )

    def test_blocks_when_spy_market_data_is_invalid(self):
        engine = self.make_engine()
        engine.get_market_snapshot.return_value["entry_data_valid"] = False
        engine.get_market_snapshot.return_value["signal_status"] = "Blocked: stale data"

        with self.assertRaisesRegex(ValueError, "stale data"):
            engine.get_s1_option_market_data(
                expiry="2026-11-05",
                option_type="CALL",
            )

        engine.get_nearest_otm_option_snapshot.assert_not_called()

    def test_propagates_invalid_option_quote(self):
        engine = self.make_engine()
        engine.get_nearest_otm_option_snapshot.side_effect = ValueError(
            "Option quote is stale"
        )

        with self.assertRaisesRegex(ValueError, "stale"):
            engine.get_s1_option_market_data(
                expiry="2026-11-05",
                option_type="CALL",
            )

        engine.option_history.get_recent_low.assert_not_called()

    def test_propagates_invalid_option_history(self):
        engine = self.make_engine()
        engine.option_history.get_recent_low.side_effect = ValueError(
            "Option history is stale"
        )

        with self.assertRaisesRegex(ValueError, "stale"):
            engine.get_s1_option_market_data(
                expiry="2026-11-05",
                option_type="CALL",
            )
