import os
import unittest

import http_utils


class HttpUtilsTest(unittest.TestCase):
    def test_safe_join_inside(self):
        self.assertEqual(http_utils.safe_join("/srv/www", "a/b.txt"), os.path.normpath("/srv/www/a/b.txt"))

    def test_safe_join_outside(self):
        self.assertIsNone(http_utils.safe_join("/srv/www", "../../etc/passwd"))

    def test_status_line(self):
        self.assertEqual(http_utils.status_line(404), "HTTP/1.1 404 Not Found")


if __name__ == "__main__":
    unittest.main()
