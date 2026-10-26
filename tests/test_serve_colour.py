"""`[console] colour` on `GET /status`: a project daemon's root, a host
daemon's `/projects/NAME/status`, and `PUT /config` patching the key.

Run: python3 -m unittest discover -s tests -p 'test_serve_colour.py' -v
"""
import sys
import unittest
from pathlib import Path

# `-m unittest tests.<name>` resolves the sibling fixtures as discovery does.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve_fixture import ServeTestCase  # noqa: E402 - after the insert

from tests.test_serve_host import HostServeCase  # noqa: E402


class ProjectStatusColourTests(ServeTestCase):
    def test_status_serves_the_configured_colour(self):
        self.seed()
        self.start('[console]\ncolour = "green"\n')

        code, _, body = self.request("GET", "/status")

        self.assertEqual((code, body["colour"]), (200, "green"))

    def test_status_serves_auto_when_no_colour_is_set(self):
        self.seed()
        self.start("")

        code, _, body = self.request("GET", "/status")

        self.assertEqual((code, body["colour"]), (200, "auto"))


class HostStatusColourTests(HostServeCase):
    def test_a_project_prefix_serves_that_projects_colour(self):
        self.config("alpha", '[console]\ncolour = "blue"\n')
        self.start()

        code, body = self.request("GET", "/projects/alpha/status")

        self.assertEqual((code, body["colour"]), (200, "blue"))


class ColourConfigEditTests(ServeTestCase):
    TOKEN = "colour-test-token"
    BEARER = {"Authorization": f"Bearer {TOKEN}"}

    def setUp(self):
        super().setUp()
        self.seed()
        token = self.root / "serve.token"
        token.write_text(self.TOKEN + "\n")
        token.chmod(0o600)
        self.before = (f'[serve]\ntoken_file = "{token}"\nconfig_edit = true\n'
                       '\n[console]\ncolour = "teal"\n')
        self.start(self.before)
        self.path = self.db.parent / "config.toml"

    def patch_colour(self, colour):
        return self.request("PUT", "/config", self.BEARER,
                            body={"patch": {"console.colour": colour}})

    def test_a_patched_colour_is_served_by_the_next_status(self):
        code, _, body = self.patch_colour("violet")
        self.assertEqual(code, 200, body)

        _, _, status = self.request("GET", "/status", self.BEARER)

        self.assertEqual(status["colour"], "violet")

    def test_a_colour_outside_the_palette_is_refused_and_the_file_kept(self):
        code, _, body = self.patch_colour("pink")

        self.assertEqual(code, 400, body)
        self.assertIn("[console] colour", body["error"])
        self.assertIn("'pink'", body["error"])
        self.assertEqual(self.path.read_text(), self.before)


if __name__ == "__main__":
    unittest.main()
