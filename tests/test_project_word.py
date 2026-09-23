"""The repository the factory works in is a "project" wherever a person or
an agent reads it (KO-617).

The string literals of the Python sources are what the command line, the
agent prompts and the daemon answers are made of, so the old word is held out
of them. Tokenized rather than grepped: identifiers, comments, docstrings and
the `{target.path}` inside an f-string are code, which a later rename
handles. A literal that is exactly `target` is kept -- the alias JSON key and
the argparse destination.

Run: python3 -m unittest tests.test_project_word -v
"""
from __future__ import annotations

import re
import token
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted((ROOT / "holophyte").glob("*.py")) + [
    ROOT / "factory.py", ROOT / "ticket_template.py"]
WORD = re.compile(r"\btargets?\b", re.IGNORECASE)
# The alias JSON key and the argparse destination, quoted either way.
ALLOWED = {'"target"', "'target'"}


def literal_text(path):
    """(line, text) for each non-docstring string literal in `path`, with an
    f-string's interpolations left out."""
    with tokenize.open(path) as source:
        tokens = list(tokenize.generate_tokens(source.readline))
    # A docstring is a string statement: its previous significant token
    # ends a line (or there is none) and a newline follows it.
    skip = {token.NL, token.COMMENT, token.INDENT, token.DEDENT}
    significant = [t for t in tokens if t.type not in skip]
    for i, tok in enumerate(significant):
        before = significant[i - 1].type if i else token.NEWLINE
        if tok.type == token.STRING:
            after = significant[i + 1].type
            if before in (token.NEWLINE, token.ENCODING) and after == token.NEWLINE:
                continue
            if tok.string in ALLOWED:
                continue
            yield tok.start[0], tok.string
        elif tok.type in (token.FSTRING_MIDDLE,
                          getattr(token, "TSTRING_MIDDLE", token.FSTRING_MIDDLE)):
            yield tok.start[0], tok.string


class ProjectWordTests(unittest.TestCase):

    def test_no_string_literal_says_target(self):
        hits = [f"{path.relative_to(ROOT)}:{line}: {text}"
                for path in SOURCES
                for line, text in literal_text(path)
                if WORD.search(text)]
        self.assertEqual(hits, [])

    def test_the_scan_sees_the_literals_it_guards(self):
        # Zero sources or zero literals would pass the check above vacuously.
        cli = [text for line, text in literal_text(ROOT / "holophyte" / "cli.py")]
        self.assertIn("repository the loop works in", " ".join(cli))
        self.assertTrue(all(path.exists() for path in SOURCES))


if __name__ == "__main__":
    unittest.main()
