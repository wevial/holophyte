"""`parse_address()`: the `--serve` address grammar."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import holophyte.serve.server  # noqa: E402 - after the sys.path insert above


class ParseAddressTests(unittest.TestCase):

    def test_a_bare_port_binds_loopback(self):
        self.assertEqual(holophyte.serve.server.parse_address("7710"),
                         ("127.0.0.1", 7710))
        self.assertEqual(holophyte.serve.server.parse_address("0"), ("127.0.0.1", 0))

    def test_host_port_is_the_pair_as_typed(self):
        self.assertEqual(holophyte.serve.server.parse_address("100.64.0.9:7710"),
                         ("100.64.0.9", 7710))
        self.assertEqual(holophyte.serve.server.parse_address("::1:8787"),
                         ("::1", 8787))

    def test_anything_else_is_refused_naming_both_shapes(self):
        for text in (":7710", "host:", "abc", "-1", "7710 ", "host:7a"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as raised:
                    holophyte.serve.server.parse_address(text)
                message = str(raised.exception)
                self.assertIn("PORT|HOST:PORT", message)
                self.assertIn(repr(text), message)
