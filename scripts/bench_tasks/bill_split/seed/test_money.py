import unittest

import money


class MoneyTest(unittest.TestCase):
    def test_format_cents(self):
        self.assertEqual(money.format_cents(1234), "$12.34")
        self.assertEqual(money.format_cents(5), "$0.05")
        self.assertEqual(money.format_cents(-250), "-$2.50")

    def test_share(self):
        self.assertEqual(money.share(1000, 1, 4), 250)


if __name__ == "__main__":
    unittest.main()
