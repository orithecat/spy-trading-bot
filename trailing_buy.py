
class TrailingBuy:
    def __init__(self, rebound_amount: float = 0.05):
        # 价格从低点反弹多少美元，触发买入信号
        self.rebound_amount = rebound_amount
        self.lowest_price = None
        self.triggered = False

    def update(self, price: float) -> bool:
        """更新最新价格；触发买入时返回 True。"""
        if self.triggered:
            return False

        if self.lowest_price is None or price < self.lowest_price:
            self.lowest_price = price
            print(f"New low: ${price:.2f}")
            return False

        rebound_price = self.lowest_price + self.rebound_amount

        if price >= round(rebound_price, 2):
            self.triggered = True
            print(
                f"BUY SIGNAL: price=${price:.2f}, "
                f"low=${self.lowest_price:.2f}"
            )
            return True

        return False



if __name__ == "__main__":
    bot = TrailingBuy(rebound_amount=0.05)

    print("Trailing Buy simulator")
    print("Enter an option price, or type q to quit.")

    while True:
        user_input = input("Option price: ").strip()

        if user_input.lower() == "q":
            print("Simulator stopped.")
            break

        try:
            price = float(user_input)

            if price <= 0:
                print("Price must be greater than zero.")
                continue

            bot.update(price)

        except ValueError:
            print("Invalid input. Enter a number or q.")
