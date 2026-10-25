"""The daemon's console pages: a moved page answers a permanent redirect."""
from __future__ import annotations

import unittest

from tests.serve_fixture import ServeTestCase


class MovedPageTest(ServeTestCase):
    """`GET /next.html` answers 301 to the console's root, keeping the query."""

    def built_console(self):
        dist = self.root / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<!doctype html><title>console</title>")
        return dist

    def assert_moved(self, path, location, headers=None):
        code, answered, _ = self.fetch("GET", path, headers)
        self.assertEqual(code, 301)
        self.assertEqual(answered["Location"], location)

    def test_next_html_redirects_to_the_root_with_its_query(self):
        self.start(console_dir=self.built_console())
        self.assert_moved("/next.html", "/")
        self.assert_moved("/next.html?project=demo", "/?project=demo")
        code, _, raw = self.fetch("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"<title>console</title>", raw)

    def test_next_html_redirects_without_a_token(self):
        token = self.root / "serve.token"
        token.write_text("static-test-token\n")
        token.chmod(0o600)
        self.start(f'[serve]\ntoken_file = "{token}"\n',
                   console_dir=self.built_console(), host="0.0.0.0")
        self.assertEqual(self.fetch("GET", "/status")[0], 401)
        self.assert_moved("/next.html", "/")

    def test_next_html_redirects_with_no_built_console(self):
        self.start()
        self.assertEqual(self.fetch("GET", "/index.html")[0], 404)
        self.assert_moved("/next.html", "/")


if __name__ == "__main__":
    unittest.main()
