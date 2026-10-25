"""The daemon's console pages: a moved page answers a permanent redirect."""
from __future__ import annotations

import unittest

from tests.serve_fixture import ServeTestCase


class MovedPageTest(ServeTestCase):
    """`GET /next.html` and `/classic.html` answer 301 to `/`, keeping the query."""

    def built_console(self):
        dist = self.root / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<!doctype html><title>console</title>")
        return dist

    def assert_moved(self, path, location, headers=None):
        code, answered, _ = self.fetch("GET", path, headers)
        self.assertEqual(code, 301)
        self.assertEqual(answered["Location"], location)

    def test_moved_pages_redirect_to_the_root_with_their_query(self):
        dist = self.built_console()
        (dist / "classic.html").write_text("<!doctype html><title>old</title>")
        self.start(console_dir=dist)
        for page in ("/next.html", "/classic.html"):
            self.assert_moved(page, "/")
            self.assert_moved(f"{page}?project=demo", "/?project=demo")
        code, _, raw = self.fetch("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"<title>console</title>", raw)

    def test_moved_pages_redirect_without_a_token(self):
        token = self.root / "serve.token"
        token.write_text("static-test-token\n")
        token.chmod(0o600)
        self.start(f'[serve]\ntoken_file = "{token}"\n',
                   console_dir=self.built_console(), host="0.0.0.0")
        self.assertEqual(self.fetch("GET", "/status")[0], 401)
        self.assert_moved("/next.html", "/")
        self.assert_moved("/classic.html", "/")

    def test_moved_pages_redirect_with_no_built_console(self):
        self.start()
        self.assertEqual(self.fetch("GET", "/index.html")[0], 404)
        self.assert_moved("/next.html", "/")
        self.assert_moved("/classic.html", "/")


if __name__ == "__main__":
    unittest.main()
