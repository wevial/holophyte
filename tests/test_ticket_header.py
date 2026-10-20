"""The `+++` TOML header: the same `Ticket` fields as the magic lines, each bad
field refused by name, one form at a time, through filing and the checker CLI.

Run: python3 -m unittest discover -s tests -p 'test_ticket_header.py' -v
"""
import contextlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert

import holophyte.cli.entry  # noqa: E402
import linear_provider  # noqa: E402
import ticket_template  # noqa: E402
from holophyte.pr import mockup  # noqa: E402
from tests.test_provider import ticket_body  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

HEADER = '''+++
ui_change = "major"
blast_radius = "high"
estimate = 45
depends_on = ["HOLO-7"]
orchestration = "subagents"
implementer = "fast"

[mockup]
url = "https://claude.ai/artifact/abc123"
approved = 2026-10-19
note = "the defaults on the row mock"

[[mockup.boards]]
url = "https://media.example/holophyte/row-dawn.png"
caption = "Row, Dawn"

[[evidence]]
file = "01-row-dawn.png"
state = "the Overview's Pull requests lane in Dawn"

[[evidence]]
file = "02-row-night.png"
state = "the same lane in Night"
+++
'''

WHAT = ("## What / Why / How\n\n**What:** Rows show their defaults.\n\n"
        "**How:** Render them on the row.\n\n")
REST = ("## In scope\n\n- The row.\n\n## Out of scope\n\n- The lane.\n\n"
        "## Acceptance criteria\n\n- [ ] Given a row, when it renders, then it "
        "shows its defaults.\n\n## Verify command(s)\n\n```\n"
        "python3 -m unittest discover -s tests -p 'test_row.py'\n```\n\n")
NOTES = "## Implementation notes\n\n- None.\n\n"
TAIL = "## Open questions\n\n- None\n"
BODY = (HEADER + "# Show the row defaults\n\n## Summary\n\nRows show defaults.\n\n"
        + WHAT + REST + NOTES + TAIL)

MAGIC = ("# Show the row defaults\n\n## Summary\n\nRows show defaults.\n\n"
         + WHAT + "**UI change:** major\n\n**Blast radius:** high\n\n"
         "## Mock-up\n\nhttps://claude.ai/artifact/abc123\n\n"
         "Approved 2026-10-19: the defaults on the row mock\n\n"
         "![Row, Dawn](https://media.example/holophyte/row-dawn.png)\n\n"
         + REST + "## Evidence\n\n- the Overview's Pull requests lane in Dawn\n"
         "- the same lane in Night\n\n" + NOTES
         + "## Estimate & dependencies\n\nEstimate: 45 min · Depends on: HOLO-7\n"
         "Orchestration: subagents\nImplementer: fast\n\n" + TAIL)


def fields(ticket):
    links = mockup.media_links(ticket.mockup_source)
    return {"ui_change": ticket.ui_change, "blast_radius": ticket.blast_radius,
            "estimate_min": ticket.estimate_min, "depends_on": ticket.depends_on,
            "orchestration": ticket.orchestration,
            "implementer": ticket.implementer,
            "evidence_states": ticket.evidence_states,
            "media_links": [(link.kind, link.url, link.caption) for link in links]}


def problems(text):
    return ticket_template.blocking(
        ticket_template.validate(ticket_template.parse(text)))


def changed(old, new):
    assert BODY.count(old) == 1, old
    return BODY.replace(old, new)


class HeaderParseTests(unittest.TestCase):
    EXPECTED = {
        "ui_change": "major", "blast_radius": "high", "estimate_min": 45,
        "depends_on": ["HOLO-7"], "orchestration": "subagents",
        "implementer": "fast",
        "evidence_states": ["the Overview's Pull requests lane in Dawn",
                            "the same lane in Night"],
        "media_links": [("snapshot", "https://media.example/holophyte/row-dawn.png",
                         "Row, Dawn")],
    }

    def test_the_header_and_the_magic_lines_give_the_same_fields(self):
        header = ticket_template.parse(BODY)
        self.assertEqual(fields(header), self.EXPECTED)
        self.assertEqual(header.evidence_files,
                         ["01-row-dawn.png", "02-row-night.png"])
        self.assertEqual(fields(ticket_template.parse(MAGIC)), self.EXPECTED)
        self.assertEqual(problems(BODY), [])
        self.assertEqual(problems(MAGIC), [])

    def test_a_run_title_line_in_front_of_the_header_changes_no_field(self):
        joined = ticket_template.parse(f"HOLO-1: Show the row defaults\n\n{BODY}")
        self.assertEqual(fields(joined), self.EXPECTED)
        self.assertEqual(joined.title, "Show the row defaults")


class HeaderProblemTests(unittest.TestCase):
    def test_each_bad_field_is_one_problem_naming_it(self):
        cases = [
            ('ui_change = "major"', 'ui_change = "big"',
             'header ui_change must be "none", "minor" or "major", not "big"'),
            ('blast_radius = "high"', 'blast_radius = "low"',
             'header blast_radius must be "high" or "medium", not "low"'),
            ("estimate = 45", 'estimate = "30 min"',
             'header estimate must be whole minutes from 1 to 90, not "30 min"'),
            ("estimate = 45", "estimate = 120",
             "header estimate must be whole minutes from 1 to 90, not 120"),
            ('depends_on = ["HOLO-7"]', 'depends_on = "HOLO-7"',
             'header depends_on must be a list of ticket keys, not "HOLO-7"'),
            ("approved = 2026-10-19", 'approved = "yesterday"',
             'header mockup.approved must be a date like 2026-10-19, '
             'not "yesterday"'),
            ('file = "02-row-night.png"', 'file = "shot.png"',
             'header evidence[2].file must read 02-slug.png or 02-slug.webm, '
             'not "shot.png"'),
            ('url = "https://media.example/holophyte/row-dawn.png"',
             'url = "https://media.example/row.gif"',
             'header mockup.boards[1].url is not a .png snapshot or a .zip '
             'bundle: "https://media.example/row.gif"'),
            ('blast_radius = "high"\n', 'blast_radius = "high"\nblast = "high"\n',
             'header field "blast" is not a ticket field'),
        ]
        for old, new, expected in cases:
            with self.subTest(new=new):
                self.assertEqual(problems(changed(old, new)), [expected])

    def test_a_webm_evidence_file_at_its_position_passes(self):
        self.assertEqual(problems(changed('file = "02-row-night.png"',
                                          'file = "02-row-night.webm"')), [])

    def test_a_broken_header_is_one_problem(self):
        bad_toml = problems(changed("estimate = 45", "estimate = "))
        self.assertEqual(len(bad_toml), 1)
        self.assertTrue(bad_toml[0].startswith("header is not valid TOML:"),
                        bad_toml)
        self.assertEqual(problems(BODY.replace("+++\n# Show", "# Show")),
                         ["header has no closing '+++' line"])
        self.assertEqual(problems(changed("estimate = 45\n", "")),
                         ["header estimate is missing: give whole minutes "
                          "from 1 to 90"])

    def test_a_magic_line_or_section_beside_a_header_is_refused(self):
        cases = [
            ("**How:** Render them on the row.\n\n",
             "**How:** Render them on the row.\n\n**UI change:** major\n\n",
             "'**UI change:**' line beside a header: give ui_change in the "
             "header only"),
            (TAIL, "## Estimate & dependencies\n\nEstimate: 45 min · Depends on: "
             "HOLO-7\n\n" + TAIL,
             "'## Estimate & dependencies' beside a header: give estimate and "
             "depends_on in the header only"),
            (NOTES, "## Evidence\n\n- the same lane in Night\n\n" + NOTES,
             "'## Evidence' beside a header: give [[evidence]] entries in the "
             "header only"),
        ]
        for old, new, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(problems(changed(old, new)), [expected])
        self.assertFalse([p for p in ticket_template.validate(
            ticket_template.parse(BODY)) if "missing section" in p])


class HeaderCheckerCliTests(unittest.TestCase):
    def test_the_checker_passes_a_header_and_refuses_a_ui_change_line_beside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            good, bad = Path(tmp) / "good.md", Path(tmp) / "bad.md"
            good.write_text(BODY)
            bad.write_text(changed("**How:** Render them on the row.\n",
                                   "**How:** Render them on the row.\n\n"
                                   "**UI change:** major\n"))
            runs = [subprocess.run(
                [sys.executable, str(ROOT / "ticket_template.py"), str(path)],
                capture_output=True, text=True, cwd=ROOT) for path in (good, bad)]
        self.assertEqual((runs[0].returncode, runs[0].stdout.splitlines()[0]),
                         (0, f"{good}: OK"))
        self.assertEqual((runs[1].returncode, runs[1].stdout.splitlines()[0]),
                         (1, f"{bad}: INVALID"))
        self.assertIn("'**UI change:**' line beside a header", runs[1].stdout)


def header_body(title, header):
    """A body filing takes, its typed fields in `header`."""
    body = ticket_body(title=title, estimate=1, verify=(
        "python3 -m unittest discover -s tests -p 'test_thing.py'"))
    return f"+++\n{header}+++\n" + body.replace(
        "## Estimate & dependencies\n\nEstimate: 1 min · Depends on: none\n\n", "")


class HeaderFilingTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def cli(self, name, text):
        path = self.root / name
        path.write_bytes(text.encode())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = holophyte.cli.entry.cli(
                [str(self.target), "--file-ticket", str(path)])
        return status, out.getvalue()

    def row(self, identifier):
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            return conn.execute(
                "SELECT timeBoxMs, dependsOn, body FROM tickets"
                " WHERE linearIdentifier = ?", (identifier,)).fetchone()

    def test_filing_reads_the_header_and_refuses_a_bad_field(self):
        self.locate('[board]\nkind = "native"\nprefix = "NAT"\n')
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        (self.target / "tests").mkdir()
        (self.target / "tests" / "test_thing.py").write_text("")
        status, printed = self.cli("T1.md", header_body("First", "estimate = 30\n"))
        self.assertEqual(status, 0, printed)
        text = header_body("Second", 'estimate = 45\ndepends_on = ["NAT-1"]\n')

        self.assertEqual(self.cli("T2.md", text)[0], 0)
        time_box, depends, body = self.row("NAT-2")
        self.assertEqual((time_box, depends), (2700000, '["NAT-1"]'))
        self.assertEqual(body.encode(), text.encode())

        status, printed = self.cli("T3.md", header_body(
            "Third", 'estimate = 45\nblast_radius = "low"\n'))
        self.assertEqual(status, 1)
        self.assertIn('header blast_radius must be "high" or "medium", not "low"',
                      printed)
        self.assertIsNone(self.row("NAT-3"))


def no_linear(*args, **kwargs):
    raise AssertionError("the command line asked Linear")


if __name__ == "__main__":
    unittest.main()
