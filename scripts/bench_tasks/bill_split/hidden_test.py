import unittest

from money import split_evenly, split_weighted


class SplitEvenlyTest(unittest.TestCase):
    def test_extra_cents_go_to_the_earliest(self):
        self.assertEqual(split_evenly(10, 3), [4, 3, 3])
        self.assertEqual(split_evenly(2, 5), [1, 1, 0, 0, 0])

    def test_exact_division(self):
        self.assertEqual(split_evenly(90, 3), [30, 30, 30])

    def test_zero_total(self):
        self.assertEqual(split_evenly(0, 3), [0, 0, 0])

    def test_single_person(self):
        self.assertEqual(split_evenly(101, 1), [101])

    def test_huge_total_is_exact(self):
        total = 10**18 + 1
        shares = split_evenly(total, 3)
        self.assertEqual(sum(shares), total)
        self.assertEqual(shares, [333333333333333334, 333333333333333334, 333333333333333333])

    def test_invalid_inputs(self):
        for total, n in ((-1, 2), (1.5, 2), ("10", 2), (True, 2), (10, 0), (10, -1), (10, 2.0), (10, None)):
            with self.subTest(total=total, n=n):
                with self.assertRaises(ValueError):
                    split_evenly(total, n)


class SplitWeightedTest(unittest.TestCase):
    def test_equal_weights(self):
        self.assertEqual(split_weighted(100, [1, 1, 1]), [34, 33, 33])

    def test_proportional(self):
        self.assertEqual(split_weighted(100, [1, 2]), [33, 67])

    def test_ties_go_to_the_earlier_person(self):
        self.assertEqual(split_weighted(10, [1, 1, 2]), [3, 2, 5])
        self.assertEqual(split_weighted(100, [1] * 7), [15, 15, 14, 14, 14, 14, 14])

    def test_largest_remainder_not_rounding(self):
        self.assertEqual(split_weighted(5, [1, 1, 1, 1, 1, 1, 1, 1]), [1, 1, 1, 1, 1, 0, 0, 0])
        self.assertEqual(split_weighted(7, [1, 1, 1, 1]), [2, 2, 2, 1])
        self.assertEqual(split_weighted(1, [1, 1]), [1, 0])
        self.assertEqual(split_weighted(3, [1, 1]), [2, 1])

    def test_zero_weight_gets_nothing(self):
        self.assertEqual(split_weighted(100, [0, 1, 1]), [0, 50, 50])
        self.assertEqual(split_weighted(1, [0, 1, 0]), [0, 1, 0])

    def test_always_adds_up(self):
        for total in (0, 1, 99, 100, 101, 12345):
            for weights in ([1], [3, 5, 7], [1, 1, 1, 1, 1, 1, 1], [0, 2, 0, 9], [10, 1, 1]):
                with self.subTest(total=total, weights=weights):
                    shares = split_weighted(total, weights)
                    self.assertEqual(sum(shares), total)
                    self.assertEqual(len(shares), len(weights))
                    self.assertTrue(all(isinstance(s, int) and s >= 0 for s in shares))

    def test_large_amounts_are_exact(self):
        total = 10**17 + 1
        shares = split_weighted(total, [1, 1, 1])
        self.assertEqual(sum(shares), total)
        self.assertEqual(shares, [33333333333333334, 33333333333333334, 33333333333333333])
        big = 10**18 + 7
        shares = split_weighted(big, [3, 5, 7])
        self.assertEqual(sum(shares), big)

    def test_large_weights(self):
        shares = split_weighted(100, [10**20, 10**20, 10**20])
        self.assertEqual(shares, [34, 33, 33])

    def test_weights_can_be_any_iterable_of_ints(self):
        self.assertEqual(split_weighted(10, (1, 1)), [5, 5])

    def test_invalid_inputs(self):
        cases = (
            (-1, [1, 1]),
            (10, []),
            (10, [0, 0]),
            (10, [1, -1]),
            (10, [1, 1.5]),
            (10, [True, 1]),
            (10.5, [1, 1]),
            (True, [1, 1]),
        )
        for total, weights in cases:
            with self.subTest(total=total, weights=weights):
                with self.assertRaises(ValueError):
                    split_weighted(total, weights)


if __name__ == "__main__":
    unittest.main()
