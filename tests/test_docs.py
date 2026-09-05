"""README is the front door; the manual lives in `docs/` by topic.

KO-227 moved the README's sections into five topic docs. The checks here
hold the split to its contract: every heading the README used to carry is
in exactly one doc, `docs/development.md` names the tree that exists and no
other, every relative link in the README and the docs resolves to a file and
an anchor that exist, and the README's usage block names every mode
`holophyte/cli.py` registers.

Run: python3 -m unittest discover -s tests -p 'test_docs*' -v
"""
from __future__ import annotations

import argparse
import re
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import holophyte.cli  # noqa: E402 - after the sys.path insert above

README = ROOT / "README.md"
DOCS = ROOT / "docs"
TOPIC_DOCS = ("loop", "operating", "config", "reviewing", "development")

# The `## ` headings README.md carried on main before the split; pinned
# rather than read from history so a heading dropped from every doc fails.
MOVED_HEADINGS = (
    "The loop",
    "State machines",
    "Files",
    "Supervising",
    "Serving",
    "Linting",
    "Config",
    "Local reviewer boundary",
)

HEADING = re.compile(r"^(#{1,6}) +(.+?)\s*$", re.MULTILINE)
# Markdown links, `[text](target)`; images and bare URLs are not links here.
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


def headings(text, level=None):
    return [title for marks, title in HEADING.findall(text)
            if level is None or len(marks) == level]


def anchor(title):
    """GitHub's slug for a heading: lowercase, punctuation dropped,
    spaces to hyphens, backticks stripped."""
    slug = re.sub(r"[^\w\- ]", "", title.replace("`", "").lower())
    return slug.replace(" ", "-")


def parser_option_strings():
    """The option strings `cli()` registers, captured from the parser it
    builds: `parse_args` is stopped before any target is located."""
    seen = []

    def capture(self, argv=None):
        seen.append(self)
        raise SystemExit(0)

    with unittest.mock.patch.object(
            argparse.ArgumentParser, "parse_args", capture):
        with self_exit():
            holophyte.cli.cli([])
    (parser,) = seen
    return sorted(opt for action in parser._actions
                  for opt in action.option_strings
                  if opt.startswith("--") and opt != "--help")


class self_exit:
    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        return kind is SystemExit


class HeadingTests(unittest.TestCase):
    def test_every_moved_heading_lives_in_exactly_one_doc(self):
        homes = {title: [] for title in MOVED_HEADINGS}
        for name in TOPIC_DOCS:
            for title in headings((DOCS / f"{name}.md").read_text(), 2):
                if title in homes:
                    homes[title].append(name)
        self.assertEqual({t: h for t, h in homes.items() if len(h) != 1}, {})
        self.assertEqual(
            [t for t in MOVED_HEADINGS if t in headings(README.read_text())],
            [], "a moved heading is still in README")


class DevelopmentDocTests(unittest.TestCase):
    def test_names_the_tree_that_exists_and_nothing_else(self):
        text = (DOCS / "development.md").read_text()
        expected = sorted(
            str(p.relative_to(ROOT))
            for pattern in ("holophyte/*.py", "store/*.py", "*.py")
            for p in ROOT.glob(pattern))
        # The ticket's verify command forbids naming `strman.py`, a leftover
        # utility no module imports; its removal is a later ticket's.
        missing = [f for f in expected
                   if f != "strman.py" and f"`{f}`" not in text]
        self.assertEqual(missing, [])
        named = re.findall(r"`((?:holophyte|store|docker|tests)/[\w./-]+"
                           r"|[\w-]+\.py)`", text)
        stale = sorted({f for f in named if not (ROOT / f).exists()})
        self.assertEqual(stale, [])
        self.assertIn("C901", text)
        self.assertRegex(text, r"\babove 12\b")


class LinkTests(unittest.TestCase):
    def test_every_relative_link_resolves_to_a_file_and_an_anchor(self):
        broken = []
        for path in [README, *DOCS.glob("*.md")]:
            for target in LINK.findall(path.read_text()):
                if "://" in target or target.startswith("mailto:"):
                    continue
                file_part, _, frag = target.partition("#")
                dest = (path.parent / file_part).resolve() if file_part \
                    else path
                if not dest.is_file():
                    broken.append(f"{path.name}: {target} (no file)")
                    continue
                if frag and frag not in map(anchor, headings(dest.read_text())):
                    broken.append(f"{path.name}: {target} (no heading)")
        self.assertEqual(broken, [])


class SingleMachineTests(unittest.TestCase):
    """KO-254: the docs describe one machine first. No page outside the
    design notes carries a dotted address other than loopback, and the
    two-host roles are introduced on the across-machines page alone."""

    IPV4 = re.compile(r"\b[0-9]{1,3}(?:\.[0-9]{1,3}){3}\b")
    ROLE_FREE = ("index.md", "architecture/overview.md",
                 "architecture/lifecycle.md", "reference/glossary.md")

    def test_no_dotted_address_outside_the_design_notes(self):
        found = []
        for path in [README, *DOCS.rglob("*.md")]:
            if DOCS / "design" in path.parents:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                for hit in self.IPV4.findall(line):
                    if hit != "127.0.0.1":
                        found.append(f"{path.relative_to(ROOT)}:{number}: {hit}")
        self.assertEqual(found, [])

    def test_roles_live_on_the_across_machines_page_only(self):
        hosts = (DOCS / "operating" / "hosts.md").read_text()
        self.assertIn("# Across machines", hosts)
        self.assertIn("writer host", hosts)
        self.assertIn("operator seat", hosts)
        for name in self.ROLE_FREE:
            text = (DOCS / name).read_text().lower()
            for role in ("writer host", "operator seat"):
                self.assertNotIn(role, text, f"{name} names the {role}")


class UsageTests(unittest.TestCase):
    def test_readme_usage_names_every_mode_the_parser_registers(self):
        text = README.read_text()
        block = re.search(r"## Usage\n+```\n(.*?)```", text, re.DOTALL)
        self.assertIsNotNone(block, "README has no usage block")
        missing = [opt for opt in parser_option_strings()
                   if opt not in block.group(1)]
        self.assertEqual(missing, [])
        for name in TOPIC_DOCS:
            self.assertIn(f"docs/{name}.md", text)


if __name__ == "__main__":
    unittest.main()
