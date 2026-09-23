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

import ast
import re
import tempfile
import token
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted((ROOT / "holophyte").glob("*.py")) + [
    ROOT / "factory.py", ROOT / "ticket_template.py"]
WORD = re.compile(r"\btargets?\b", re.IGNORECASE)
# The alias JSON key and the argparse destination.
ALLOWED = "target"
STARTS = {token.FSTRING_START, getattr(token, "TSTRING_START", token.FSTRING_START)}
MIDDLES = {token.FSTRING_MIDDLE, getattr(token, "TSTRING_MIDDLE", token.FSTRING_MIDDLE)}


def decoded(literal):
    """A STRING token's value, escapes resolved: `"\\ntarget"` is a newline
    and then the word, which the raw spelling's `n` would hide."""
    value = ast.literal_eval(literal)
    return value.decode("latin-1") if isinstance(value, bytes) else value


def unescaped(middle):
    """An f-string's literal part with its escapes resolved; non-latin-1
    characters survive the round trip as `\\u` escapes."""
    return middle.encode("latin-1", "backslashreplace").decode("unicode_escape")


def literal_text(path):
    """(line, text) for each non-docstring string literal in `path`, escapes
    decoded and an f-string's interpolations left out."""
    with tokenize.open(path) as source:
        tokens = list(tokenize.generate_tokens(source.readline))
    # A docstring is a string statement: its previous significant token
    # ends a line (or there is none) and a newline follows it.
    skip = {token.NL, token.COMMENT, token.INDENT, token.DEDENT}
    significant = [t for t in tokens if t.type not in skip]
    raw = []  # one entry per open f-string: is it a raw one?
    for i, tok in enumerate(significant):
        before = significant[i - 1].type if i else token.NEWLINE
        if tok.type == token.STRING:
            after = significant[i + 1].type
            if before in (token.NEWLINE, token.ENCODING) and after == token.NEWLINE:
                continue
            text = decoded(tok.string)
            if text != ALLOWED:
                yield tok.start[0], text
        elif tok.type in STARTS:
            raw.append("r" in tok.string.lower())
        elif tok.type in MIDDLES:
            yield tok.start[0], tok.string if raw[-1] else unescaped(tok.string)
        elif tok.type in (token.FSTRING_END,
                          getattr(token, "TSTRING_END", token.FSTRING_END)):
            raw.pop()


def hits(path):
    return [f"{path.name}:{line}: {text!r}"
            for line, text in literal_text(path) if WORD.search(text)]


class ProjectWordTests(unittest.TestCase):

    def test_no_string_literal_says_target(self):
        self.assertEqual([hit for path in SOURCES for hit in hits(path)], [])

    def test_an_escape_before_the_word_does_not_hide_it(self):
        # Review of KO-617: the raw spelling `\ntarget` has no word boundary
        # before the word, so the check must read decoded text. A raw
        # f-string's `\n` stays two characters, so its `ntarget` is not the
        # word; the code a later rename handles stays out of the count.
        sample = ('"""A target in a docstring is code."""\n'
                  'x = {"target": 1}\n'
                  'print("\\ntarget repository")\n'
                  'print(f"\\ntarget {path}")\n'
                  'print(rf"\\ntarget {path}")\n'
                  'print(f"{target.path} ok")\n')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.py"
            path.write_text(sample)
            found = [line for line, text in literal_text(path)
                     if WORD.search(text)]
        self.assertEqual(found, [3, 4])

    def test_the_scan_sees_the_literals_it_guards(self):
        # Zero sources or zero literals would pass the check above vacuously.
        cli = [text for line, text in literal_text(ROOT / "holophyte" / "cli.py")]
        self.assertIn("repository the loop works in", " ".join(cli))
        self.assertTrue(all(path.exists() for path in SOURCES))


if __name__ == "__main__":
    unittest.main()
