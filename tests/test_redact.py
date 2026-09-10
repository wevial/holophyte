"""The redaction rule of `holophyte.redact` (KO-356), on the text alone.

`GET /config` serves `redact(text)`; `tests/test_serve.py` witnesses the
route. This file witnesses the rule itself on a document that puts
secrets at every depth the loader's file can: a top-level pair, nested
tables, dotted keys, an inline table, and arrays of tables. `tomllib`
over the result is the oracle, so the test asserts what the redacted
document *means*, not what the scanner did to produce it.
"""

import tomllib
import unittest

from holophyte.redact import REDACTED, redact, restore

DOCUMENT = '''\
# top-level secret and a path that only looks like one
token = "S-top"
token_file = "/run/secrets/serve.token"

[linear]
api_key = "S-linear"   # comment stays
team = "KO"

[agents.provider]
key = 'S-literal'
model = "m"
nested.deep.api_key = "S-dotted"
board = { api_key = "S-inline", team = "t" }

[[many]]
name = "first"
token = "S-first"

[[many]]
name = "second"
token = "S-second"

[[many.inner]]
key = """S-multi
line"""
'''

SECRETS = ("S-top", "S-linear", "S-literal", "S-dotted", "S-inline",
           "S-first", "S-second", "S-multi")


class RedactRule(unittest.TestCase):

    def test_every_secret_at_any_depth_reads_redacted(self):
        shown = redact(DOCUMENT)
        for secret in SECRETS:
            self.assertNotIn(secret, shown, secret)
        parsed = tomllib.loads(shown)
        self.assertEqual(parsed["token"], REDACTED)
        self.assertEqual(parsed["linear"]["api_key"], REDACTED)
        provider = parsed["agents"]["provider"]
        self.assertEqual(provider["key"], REDACTED)
        self.assertEqual(provider["nested"]["deep"]["api_key"], REDACTED)
        self.assertEqual(provider["board"]["api_key"], REDACTED)
        self.assertEqual([m["token"] for m in parsed["many"]],
                         [REDACTED, REDACTED])
        self.assertEqual(parsed["many"][1]["inner"][0]["key"], REDACTED)

    def test_a_table_under_a_secret_key_is_redacted_whole(self):
        """`api_key = { token = "secret" }` is one value: the scanner
        replaces the whole table, and the parsed check accepts the
        placeholder where the table was rather than demanding the pair
        inside it. Such a file parses and the loader leaves an unknown
        table alone, so it must be served, not 500."""
        text = '[extra]\napi_key = { token = "secret" }\n'
        shown = redact(text)
        self.assertNotIn("secret", shown)
        self.assertEqual(tomllib.loads(shown)["extra"]["api_key"], REDACTED)

    def test_a_secret_named_table_is_redacted_however_it_is_written(self):
        """The same table as a `[header]`, a dotted key and an `[[array]]`
        header (the review's regression): each pair under it is a secret,
        as the inline form already was, and the redacted text still
        restores."""
        text = ('[extra.api_key]\nvalue = "S-header"\nother = 1\n'
                '[extra2]\napi_key.value = "S-dotted"\n'
                '[[extra3.api_key]]\nvalue = "S-array"\n')
        shown = redact(text)
        for secret in ("S-header", "S-dotted", "S-array"):
            self.assertNotIn(secret, shown, secret)
        parsed = tomllib.loads(shown)
        self.assertEqual(parsed["extra"]["api_key"],
                         {"value": REDACTED, "other": REDACTED})
        self.assertEqual(parsed["extra2"]["api_key"]["value"], REDACTED)
        self.assertEqual(parsed["extra3"]["api_key"][0]["value"], REDACTED)
        self.assertEqual(tomllib.loads(restore(shown, text)),
                         tomllib.loads(text))

    def test_paths_and_plain_values_stay_and_only_values_move(self):
        shown = redact(DOCUMENT)
        parsed = tomllib.loads(shown)
        original = tomllib.loads(DOCUMENT)
        self.assertEqual(parsed["token_file"], original["token_file"])
        self.assertEqual(parsed["linear"]["team"], "KO")
        self.assertEqual(parsed["agents"]["provider"]["model"], "m")
        self.assertEqual(parsed["agents"]["provider"]["board"]["team"], "t")
        self.assertEqual([m["name"] for m in parsed["many"]],
                         ["first", "second"])
        # The comment beside a redacted value survives: only the value's
        # span was rewritten.
        self.assertIn('api_key = "[redacted]"   # comment stays', shown)

    def test_restore_puts_each_secret_back_by_position(self):
        shown = redact(DOCUMENT)
        edited = shown.replace('team = "KO"', 'team = "XY"').replace(
            'token = "[redacted]"\n\n[[many]]\nname = "second"',
            'token = "S-rewritten"\n\n[[many]]\nname = "second"')
        back = tomllib.loads(restore(edited, DOCUMENT))
        self.assertEqual(back["linear"]["team"], "XY")
        self.assertEqual(back["token"], "S-top")
        self.assertEqual(back["agents"]["provider"]["nested"]["deep"]["api_key"],
                         "S-dotted")
        self.assertEqual([m["token"] for m in back["many"]],
                         ["S-rewritten", "S-second"])
        self.assertEqual(back["many"][1]["inner"][0]["key"], "S-multi\nline")

    def test_a_placeholder_for_a_value_the_file_never_held_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            restore(DOCUMENT + '\n[other]\ntoken = "[redacted]"\n', DOCUMENT)
        self.assertIn("[other] token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
