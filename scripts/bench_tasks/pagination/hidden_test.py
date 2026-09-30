import unittest

from catalog import paginate


class PaginateTest(unittest.TestCase):
    def test_middle_page(self):
        result = paginate(list(range(25)), page=2, per_page=10)
        self.assertEqual(result["items"], list(range(10, 20)))
        self.assertEqual(result["page"], 2)
        self.assertEqual(result["per_page"], 10)
        self.assertEqual(result["total"], 25)
        self.assertEqual(result["total_pages"], 3)
        self.assertTrue(result["has_prev"])
        self.assertTrue(result["has_next"])

    def test_defaults_give_first_page_of_ten(self):
        result = paginate(list(range(25)))
        self.assertEqual(result["items"], list(range(10)))
        self.assertEqual(result["page"], 1)
        self.assertEqual(result["per_page"], 10)
        self.assertFalse(result["has_prev"])
        self.assertTrue(result["has_next"])

    def test_last_partial_page(self):
        result = paginate(list(range(25)), page=3, per_page=10)
        self.assertEqual(result["items"], [20, 21, 22, 23, 24])
        self.assertFalse(result["has_next"])
        self.assertTrue(result["has_prev"])

    def test_exact_multiple_has_no_extra_page(self):
        result = paginate(list(range(20)), page=2, per_page=10)
        self.assertEqual(result["total_pages"], 2)
        self.assertEqual(result["items"], list(range(10, 20)))
        self.assertFalse(result["has_next"])

    def test_page_past_the_end_is_empty_not_an_error(self):
        result = paginate(list(range(20)), page=3, per_page=10)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total_pages"], 2)
        self.assertFalse(result["has_next"])
        self.assertTrue(result["has_prev"])
        far = paginate(list(range(25)), page=99, per_page=10)
        self.assertEqual(far["items"], [])
        self.assertFalse(far["has_next"])

    def test_empty_list(self):
        result = paginate([], page=1, per_page=10)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["has_next"])
        self.assertFalse(result["has_prev"])

    def test_per_page_larger_than_total(self):
        result = paginate([1, 2, 3], page=1, per_page=50)
        self.assertEqual(result["items"], [1, 2, 3])
        self.assertEqual(result["total_pages"], 1)
        self.assertFalse(result["has_next"])

    def test_single_item_pages(self):
        result = paginate(["a", "b", "c"], page=3, per_page=1)
        self.assertEqual(result["items"], ["c"])
        self.assertEqual(result["total_pages"], 3)
        self.assertFalse(result["has_next"])
        self.assertTrue(result["has_prev"])

    def test_invalid_page_or_per_page(self):
        for bad in (0, -1, 1.0, 2.5, "1", None, True):
            with self.subTest(page=bad):
                with self.assertRaises(ValueError):
                    paginate([1, 2, 3], page=bad, per_page=2)
            with self.subTest(per_page=bad):
                with self.assertRaises(ValueError):
                    paginate([1, 2, 3], page=1, per_page=bad)

    def test_items_is_a_list_and_input_is_untouched(self):
        source = tuple(range(5))
        result = paginate(source, page=1, per_page=2)
        self.assertIsInstance(result["items"], list)
        self.assertEqual(source, (0, 1, 2, 3, 4))


if __name__ == "__main__":
    unittest.main()
