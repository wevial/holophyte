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
import os
import re
import subprocess
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
        # KO-520 promotes Config to the page title above per-table sections.
        self.assertTrue((DOCS / "config.md").read_text().startswith("# Config\n"))
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
        self.assertIn("factory.py project", block.group(1))
        for name in TOPIC_DOCS:
            self.assertIn(f"docs/{name}.md", text)


def section(text, title):
    """The body under the `## title` heading, up to the next `## `."""
    return text.split(f"\n## {title}\n", 1)[1].split("\n## ", 1)[0]


class CliReferenceTests(unittest.TestCase):
    """KO-626: `docs/reference/cli.md` has a row for every mode the parser
    registers and every `project` verb the real entry point lists."""

    PAGE = DOCS / "reference" / "cli.md"

    def test_names_every_option_and_project_verb(self):
        text = self.PAGE.read_text()
        # Exact option tokens from the tables' Invocation cells only, so a
        # mention in the prose, or `--close` inside `--close-pr`, is no row.
        cells = " ".join(re.split(r"(?<!\\)\|", line)[1]
                         for line in text.splitlines()
                         if line.startswith("| `"))
        invoked = set(re.findall(r"--[a-z][\w-]*", cells))
        missing = [opt for opt in parser_option_strings()
                   if opt not in invoked]
        self.assertEqual(missing, [], "flags with no row on the page")
        result = subprocess.run(
            [sys.executable, str(ROOT / "factory.py"), "project", "--help"],
            capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        choices = re.search(r"\{([\w,]+)\}", result.stdout)
        self.assertIsNotNone(choices, result.stdout)
        verbs = choices.group(1).split(",")
        self.assertIn("add", verbs)
        self.assertEqual([v for v in verbs if f"`project {v} " not in cells],
                         [], "project verbs with no row on the page")
        self.assertIn("--store", invoked)

    def test_failure_lines_board_needs_and_exit_codes(self):
        text = self.PAGE.read_text()
        for mode in ("`--report PROJECT`", "`--sweep PROJECT`"):
            (row,) = [line for line in text.splitlines()
                      if line.startswith(f"| {mode}")]
            self.assertIn("`failures KIND: N`", row)
        board = section(text, "Startup checks").split("need a `[board]`")[0]
        for mode in ("`--close`", "`--worker`"):
            self.assertIn(mode, board)
        (exit_1,) = [line for line in text.splitlines()
                     if line.startswith("| 1 |")]
        for refusal in ("pause", "resume", "close-out", "hold", "`project`"):
            self.assertIn(refusal, exit_1)


class OperatingIntroTests(unittest.TestCase):
    """KO-626: the pause section is about pausing; the merge-gate, re-point
    and send-back notes that once followed the intro have their own heading."""

    def test_pause_section_holds_only_pausing(self):
        text = (DOCS / "operating.md").read_text()
        pause = section(text, "Pause one run at its next safe point")
        self.assertNotIn("--repoint", pause)
        homes = [title for title in headings(text, 2)
                 if "merge gate conflict" in section(text, title)]
        self.assertEqual(len(homes), 1, homes)
        notes = section(text, homes[0])
        for flag in ("--repoint", "--babysit"):
            self.assertIn(flag, notes)
        self.assertNotIn("--pause", notes)
        self.assertNotIn("--abort", notes)


class PullRequestTemplateTests(unittest.TestCase):
    """KO-430: the repository's pull request template -- the file the
    written turn fills; GitHub itself applies
    it only to pull requests opened in the web UI."""

    TEMPLATE = ROOT / ".github" / "pull_request_template.md"

    def test_the_template_has_the_house_sections(self):
        text = self.TEMPLATE.read_text()
        self.assertEqual(headings(text, 2),
                         ["Summary", "Why", "Changes", "Diagram",
                          "Verification"])
        # Changes is an at-a-glance table, and the last line is the
        # `Linear:` marker the loop completes -- no footer after it.
        changes = text.split("## Changes", 1)[1].split("##", 1)[0]
        self.assertIn("|", changes)
        self.assertEqual(text.rstrip().splitlines()[-1], "Linear:")


class BabysitterTests(unittest.TestCase):
    """KO-373: the pass over an open pull request is the babysitter wherever
    the operator reads it; KO-374 renamed the store's action value and
    `store.babysit()` with it, so the old word is gone from everything the
    operator reads."""

    def test_the_old_word_is_gone_from_what_the_operator_reads(self):
        hits = subprocess.run(
            ["git", "grep", "-in", "shepherd", "--", "docs", "README.md",
             "holophyte"], cwd=ROOT, capture_output=True, text=True).stdout
        self.assertEqual(hits.splitlines(), [])

    def test_the_cli_and_glossary_pages_name_the_babysitter(self):
        self.assertIn("--babysit", (DOCS / "reference" / "cli.md").read_text())
        self.assertRegex((DOCS / "reference" / "glossary.md").read_text(),
                         r"\*\*Babysitter\.\*\*")


class ProjectWordTests(unittest.TestCase):
    """KO-618: the manual calls the repository the factory works on a
    project, as `factory.py project add` and the store's `projects` table
    do. Mermaid node names follow the code, and the code type `Target`, its
    module and the JSON alias key `target` keep the old word until later
    tickets rename them; `docs/design/` holds dated records."""

    maxDiff = None
    OLD_WORD = re.compile(r"\btargets?\b", re.IGNORECASE)
    # `holophyte/target.py` is permitted beyond the ticket's three forms:
    # the ticket leaves the module path to the type rename
    # (394-project-rename-c-type.md), and DevelopmentDocTests requires
    # development.md to name every module that exists. That rename retires
    # this exception.
    PERMITTED = re.compile(
        r"```mermaid\n.*?```|`Target`|`target`|\"target\""
        r"|`holophyte/target\.py`", re.DOTALL)

    def test_the_old_word_is_gone_outside_the_design_notes(self):
        found = []
        for path in [README, ROOT / "AGENTS.md", *DOCS.rglob("*.md")]:
            if DOCS / "design" in path.parents:
                continue
            # Blank a permitted span but keep its newlines, so line
            # numbers still point into the file.
            text = self.PERMITTED.sub(
                lambda m: "\n" * m.group(0).count("\n"), path.read_text())
            for number, line in enumerate(text.splitlines(), 1):
                if self.OLD_WORD.search(line):
                    found.append(f"{path.relative_to(ROOT)}:{number}: {line}")
        self.assertEqual(found, [])

    def test_the_glossary_defines_project_and_the_board_as_linear(self):
        text = (DOCS / "reference" / "glossary.md").read_text()
        self.assertRegex(text, r"\*\*Project\.\*\*")
        self.assertNotRegex(text, r"\*\*Target\.\*\*")
        board = re.search(r"\*\*Board\.\*\*(.*?)\n\n", text, re.DOTALL)
        self.assertIsNotNone(board, "glossary has no Board entry")
        self.assertIn("Linear project", board.group(1))

    def test_the_cli_page_takes_a_project(self):
        text = (DOCS / "reference" / "cli.md").read_text()
        self.assertIn("`python3 factory.py [MODE] PROJECT`", text)
        modes = re.search(r"\| Invocation \| Does \| Touches \|\n"
                          r"\| --- \| --- \| --- \|\n((?:\|.*\n)+)", text)
        self.assertIsNotNone(modes, "cli.md has no mode table")
        rows = modes.group(1).splitlines()
        self.assertTrue(rows)
        wrong = [row.split(" | ")[0] for row in rows
                 if not row.split(" | ")[0].endswith(" PROJECT`")]
        self.assertEqual(wrong, [])

    def test_the_http_page_names_project_the_key_and_target_its_alias(self):
        text = re.sub(r"\s+", " ",
                      (DOCS / "reference" / "http.md").read_text())
        for route in ("/status", "/attention"):
            section = text.split(f"## `GET {route}`", 1)[1].split(" ## ", 1)[0]
            self.assertIn('"project": "/path/to/repo"', section, route)
            self.assertRegex(section, r"`project` is the (?:project path|"
                             r"repository the daemon serves)", route)
            self.assertRegex(section, r"`target` (?:is )?(?:its |a )?"
                             r"deprecated alias[^.]*same value", route)


class ArchitectureTruthTests(unittest.TestCase):
    """KO-593: the architecture pages say what the store and the daemon do.
    The schema version is read from the constant, so a bump that leaves the
    page behind fails here; the other checks are phrase present or absent."""

    ARCH = DOCS / "architecture"
    # What the pages said before the action endpoints and the per-ticket
    # lease; each is false of the code now.
    RETIRED = ("cannot write", "only writers", "supervisor only",
               "one run per project")

    def test_data_page_prints_the_current_schema_version(self):
        import store.schema
        text = (self.ARCH / "data.md").read_text()
        printed = re.search(r"`PRAGMA user_version`, currently (\d+)", text)
        self.assertIsNotNone(printed, "data.md prints no schema version")
        self.assertEqual(int(printed.group(1)), store.schema.SCHEMA_VERSION)

    def test_no_page_says_the_daemon_cannot_write_or_names_two_writers(self):
        found = [f"{path.name}: {phrase}"
                 for path in sorted(self.ARCH.glob("*.md"))
                 for phrase in self.RETIRED
                 if phrase in re.sub(r"\s+", " ", path.read_text())]
        self.assertEqual(found, [])
        data = (self.ARCH / "data.md").read_text()
        self.assertRegex(data, r"\| `tickets` \|[^\n]*`activeRunId` is the lease")
        self.assertNotRegex(data, r"\| `projects` \|[^\n]*`activeRunId` is the lease")
        for name in ("processes.md", "components.md", "overview.md"):
            text = re.sub(r"\s+", " ", (self.ARCH / name).read_text())
            self.assertIn("store API", text, name)

    def test_serve_unit_names_the_bearer_token_as_its_boundary(self):
        unit = (ROOT / "deploy" / "holophyte-serve@.service").read_text()
        self.assertIn("bearer token", unit)
        self.assertIn("[serve] token_file", unit)
        self.assertNotIn("no authentication", unit)


class DaemonWritesTests(unittest.TestCase):
    """KO-627: `--serve` reads by default and writes through two opt-ins,
    `[serve] actions` and `[serve] config_edit`, so neither `--help` nor the
    manual calls it read-only. `docs/design/` holds dated records and
    `docs/architecture/` is ArchitectureTruthTests' (KO-593)."""

    # What the pages said before the action endpoints and `PUT /config`.
    RETIRED = ("read-only JSON daemon", "read-only HTTP daemon",
               "A read-only daemon", "The daemon is read-only",
               "serving its state read-only")

    def test_no_page_calls_the_daemon_read_only(self):
        # A phrase may wrap, so its words match across any whitespace; the
        # hit is reported at the line it starts on.
        patterns = [re.compile(r"\s+".join(map(re.escape, phrase.split())))
                    for phrase in self.RETIRED]
        found = []
        for path in [README, *DOCS.rglob("*.md")]:
            if {DOCS / "design", DOCS / "architecture"} & set(path.parents):
                continue
            text = path.read_text()
            found += [f"{path.relative_to(ROOT)}:"
                      f"{text.count(chr(10), 0, hit.start()) + 1}: "
                      f"{' '.join(hit.group(0).split())}"
                      for pattern in patterns
                      for hit in pattern.finditer(text)]
        self.assertEqual(found, [])

    def test_help_names_the_actions_opt_in(self):
        # A wide COLUMNS keeps argparse from wrapping an entry mid-phrase.
        help_text = subprocess.run(
            [sys.executable, "factory.py", "--help"], cwd=ROOT,
            env={**os.environ, "COLUMNS": "1000"}, capture_output=True,
            text=True, check=True).stdout
        entry = re.search(r"^  --serve .*?(?=^  -)", help_text,
                          re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(entry, help_text)
        serve = " ".join(entry.group(0).split())
        self.assertNotIn("writes nothing", serve)
        self.assertNotIn("read-only", serve)
        self.assertIn("[serve] actions", serve)

    def test_cli_row_and_http_opening_link_the_writing_routes(self):
        cli = (DOCS / "reference" / "cli.md").read_text()
        row = re.search(r"^\| `--serve PORT PROJECT` \|.*$", cli, re.MULTILINE)
        self.assertIsNotNone(row, "cli.md has no --serve row")
        http = (DOCS / "reference" / "http.md").read_text()
        opening = http.split("\n## ", 1)[0]
        for name, text in (("cli.md", row.group(0)), ("http.md", opening)):
            text = " ".join(text.split())
            self.assertIn("](daemon.md)", text, name)
            self.assertIn("POST /actions/", text, name)
            self.assertIn("PUT /config", text, name)

    def test_http_preflight_names_the_methods_serve_sends(self):
        auth = section((DOCS / "reference" / "http.md").read_text(),
                       "Authentication")
        self.assertIn("`Access-Control-Allow-Methods: GET, POST, PUT`",
                      " ".join(auth.split()))


if __name__ == "__main__":
    unittest.main()
