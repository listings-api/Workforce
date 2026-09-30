import unittest

import catalog


class CatalogTest(unittest.TestCase):
    def test_search_ignores_case(self):
        self.assertEqual([p["id"] for p in catalog.search(catalog.PRODUCTS, "product 007")], [7])

    def test_cheapest(self):
        self.assertEqual([p["id"] for p in catalog.cheapest(catalog.PRODUCTS, 2)], [1, 2])


if __name__ == "__main__":
    unittest.main()
