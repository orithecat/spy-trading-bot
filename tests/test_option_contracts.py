import unittest

from option_contracts import OptionContractFinder


def contract(symbol, option_type, strike):
    return {
        "symbol": symbol,
        "underlying_symbol": "SPY",
        "expiration_date": "2026-11-05",
        "option_type": option_type,
        "status": "LISTING",
        "strike_price": str(strike),
        "strike_price_numeric": float(strike),
    }


class TestOptionContractFinder(unittest.TestCase):

    def test_select_nearest_otm_call(self):
        contracts = [
            contract("CALL760", "CALL", 760),
            contract("CALL761", "CALL", 761),
            contract("CALL762", "CALL", 762),
        ]

        result = OptionContractFinder.select_otm_contract(
            contracts, "CALL", 760.3
        )

        self.assertEqual(result["symbol"], "CALL761")

    def test_select_nearest_otm_put(self):
        contracts = [
            contract("PUT759", "PUT", 759),
            contract("PUT760", "PUT", 760),
            contract("PUT761", "PUT", 761),
        ]

        result = OptionContractFinder.select_otm_contract(
            contracts, "PUT", 760.3
        )

        self.assertEqual(result["symbol"], "PUT760")

    def test_atm_is_not_selected(self):
        contracts = [
            contract("CALL760", "CALL", 760),
        ]

        with self.assertRaises(ValueError):
            OptionContractFinder.select_otm_contract(
                contracts, "CALL", 760
            )

    def test_no_otm_contract_raises(self):
        contracts = [
            contract("PUT761", "PUT", 761),
        ]

        with self.assertRaises(ValueError):
            OptionContractFinder.select_otm_contract(
                contracts, "PUT", 760
            )

    def test_invalid_option_type_raises(self):
        with self.assertRaises(ValueError):
            OptionContractFinder.select_otm_contract(
                [], "INVALID", 760
            )

    def test_invalid_underlying_price_raises(self):
        with self.assertRaises(ValueError):
            OptionContractFinder.select_otm_contract(
                [], "CALL", 0
            )


if __name__ == "__main__":
    unittest.main()
