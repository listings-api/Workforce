import os
import tempfile
import unittest

import static_server


def serve(root, url_path):
    status, content_type, body = static_server.serve(root, url_path)
    return status, content_type.split(";")[0].strip(), body


class ServeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = os.path.realpath(self.tmp.name)
        self.root = os.path.join(base, "www")
        os.makedirs(os.path.join(self.root, "guide"))
        os.makedirs(os.path.join(self.root, "empty"))
        os.makedirs(os.path.join(base, "www-private"))
        self.write(self.root, "index.html", "<h1>home</h1>")
        self.write(self.root, "style.css", "body{}")
        self.write(self.root, "data.unknownext", "raw")
        self.write(self.root, "notes..v2.txt", "dots")
        self.write(self.root, "file name.txt", "spaced")
        self.write(os.path.join(self.root, "guide"), "index.html", "<h1>guide</h1>")
        self.write(os.path.join(self.root, "guide"), "intro.html", "<p>intro</p>")
        self.write(base, "secret.txt", "top secret")
        self.write(os.path.join(base, "www-private"), "secret.txt", "private secret")
        os.symlink(os.path.join(base, "secret.txt"), os.path.join(self.root, "leak.txt"))
        os.symlink(os.path.join(self.root, "guide", "intro.html"), os.path.join(self.root, "alias.html"))

    def write(self, folder, name, text):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def assertRefused(self, url_path):
        status, _, body = serve(self.root, url_path)
        self.assertEqual(status, 403, url_path)
        self.assertNotIn(b"secret", body)

    def test_plain_file(self):
        status, content_type, body = serve(self.root, "/style.css")
        self.assertEqual((status, content_type, body), (200, "text/css", b"body{}"))

    def test_html_file_in_folder(self):
        status, content_type, body = serve(self.root, "/guide/intro.html")
        self.assertEqual((status, content_type, body), (200, "text/html", b"<p>intro</p>"))

    def test_unknown_extension(self):
        status, content_type, body = serve(self.root, "/data.unknownext")
        self.assertEqual((status, content_type, body), (200, "application/octet-stream", b"raw"))

    def test_root_serves_index(self):
        status, content_type, body = serve(self.root, "/")
        self.assertEqual((status, content_type, body), (200, "text/html", b"<h1>home</h1>"))

    def test_folder_with_and_without_slash(self):
        for path in ("/guide/", "/guide"):
            with self.subTest(path=path):
                self.assertEqual(serve(self.root, path), (200, "text/html", b"<h1>guide</h1>"))

    def test_folder_without_index_is_404(self):
        self.assertEqual(serve(self.root, "/empty/")[0], 404)
        self.assertEqual(serve(self.root, "/empty")[0], 404)

    def test_missing_file_is_404(self):
        self.assertEqual(serve(self.root, "/nope.html")[0], 404)

    def test_query_and_fragment_are_ignored(self):
        self.assertEqual(serve(self.root, "/style.css?v=3")[0], 200)
        self.assertEqual(serve(self.root, "/style.css#top")[0], 200)
        self.assertEqual(serve(self.root, "/guide/?a=1&b=2")[2], b"<h1>guide</h1>")

    def test_percent_encoded_file_name(self):
        self.assertEqual(serve(self.root, "/file%20name.txt"), (200, "text/plain", b"spaced"))

    def test_dotted_file_names_are_not_traversal(self):
        self.assertEqual(serve(self.root, "/notes..v2.txt"), (200, "text/plain", b"dots"))

    def test_dot_dot_traversal_is_refused(self):
        for path in (
            "/../secret.txt",
            "/guide/../../secret.txt",
            "/a/../../secret.txt",
            "/../../../../../../etc/passwd",
            "/..",
            "/../",
        ):
            with self.subTest(path=path):
                self.assertRefused(path)

    def test_encoded_traversal_is_refused(self):
        for path in (
            "/%2e%2e/secret.txt",
            "/%2E%2E/secret.txt",
            "/..%2fsecret.txt",
            "/guide/%2e%2e/%2e%2e/secret.txt",
            "/%2e%2e%2fsecret.txt",
        ):
            with self.subTest(path=path):
                self.assertRefused(path)

    def test_sibling_folder_sharing_the_root_prefix_is_refused(self):
        self.assertRefused("/../www-private/secret.txt")
        self.assertRefused("/%2e%2e/www-private/secret.txt")

    def test_symlink_pointing_outside_is_refused(self):
        self.assertRefused("/leak.txt")

    def test_symlink_pointing_inside_is_served(self):
        self.assertEqual(serve(self.root, "/alias.html"), (200, "text/html", b"<p>intro</p>"))

    def test_double_slash_is_not_an_absolute_path(self):
        status, _, body = serve(self.root, "//etc/passwd")
        self.assertIn(status, (403, 404))
        self.assertNotIn(b"root:", body)

    def test_doubly_encoded_dots_do_not_reach_the_parent(self):
        status, _, body = serve(self.root, "/%252e%252e/secret.txt")
        self.assertNotEqual(status, 200)
        self.assertNotIn(b"secret", body)

    def test_never_raises_on_odd_input(self):
        for path in ("", "/%00", "/a%00b.txt", "/" + "x" * 5000, "/%ff%fe", "?only=query", "/guide/intro.html/"):
            with self.subTest(path=path):
                status, content_type, body = serve(self.root, path)
                self.assertIn(status, (200, 400, 403, 404))
                self.assertIsInstance(body, bytes)

    def test_error_bodies_are_plain_text_bytes(self):
        status, content_type, body = serve(self.root, "/nope.html")
        self.assertIsInstance(body, bytes)
        self.assertTrue(body)


if __name__ == "__main__":
    unittest.main()
