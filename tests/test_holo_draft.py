"""`holo draft` prints `ticketTemplate.md` naming the project's implementer
profiles, or a filed ticket's body with its magic lines moved into a `+++`
header, read from the project's real store.

Run: python3 -m unittest discover -s tests -p 'test_holo_draft.py' -v
"""
import contextlib
import io
import subprocess
import unittest
from pathlib import Path

import store
import store.board
import store.tickets
from holophyte.holo.cli import main
from holophyte.tickets import ticket_header, ticket_template
from tests.config_fixture import ConfigTestCase

ROOT = Path(__file__).resolve().parent.parent
BOARD = '[board]\nkind = "native"\nprefix = "NAT"\n'
PROFILES = ('[agents.implementer_profiles.fast]\nharness = "claude"\n'
            '[agents.implementer_profiles.deep]\nharness = "claude"\n')
TOP = """\
# Export orders

## Summary

Add a CSV export endpoint for the orders list.

## What / Why / How

**What:** GET /orders.csv streams the current user's orders as CSV.

**Why:** Ops needs orders in spreadsheets without database access.

**How:** Reuse the orders query service and stream via the csv module.

"""
MIDDLE = """\
## In scope

- CSV serialization of the orders list

## Out of scope

- Excel-specific formatting

## Acceptance criteria

- [ ] Given 3 orders, when GET /orders.csv, then 4 lines including header.

## Verify command(s)

```
python3 -m unittest test_orders_export
```

"""
NOTES = """\
## Implementation notes

- Endpoint lives beside the other order routes.

"""
OPEN = """\
## Open questions

- None
"""
FIELD_LINES = "**UI change:** minor\n\n**Blast radius:** medium\n\n"
ESTIMATE = ("## Estimate & dependencies\n\n"
            "Estimate: 30 min · Depends on: none\nImplementer: fast\n\n")
MAGIC = TOP + FIELD_LINES + MIDDLE + NOTES + ESTIMATE + OPEN
MOCKUP = """\
## Mock-up

https://claude.ai/artifact/abc123

Approved 2026-10-19: the defaults

![Row, Dawn](https://media.example/row-dawn.png)

https://media.example/row.zip

"""
EVIDENCE = """\
## Evidence

- `01-row-dawn.png`: the row in Dawn
- The row in Night!
- `03-flow.webm`: the send-back flow

"""


def holo(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(args))
        except SystemExit as stopped:
            code = stopped.code
    return code, out.getvalue(), err.getvalue()


class DraftCase(ConfigTestCase):
    def project_with(self, config):
        project = self.locate(BOARD + config)
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        conn = store.open(str(project.store_path))
        self.addCleanup(conn.close)
        store.init(conn)
        self.conn = conn
        self.project_id = store.tickets.ensure_project(conn, "native:NAT",
                                                       self.target)
        return project

    def filed(self, body, column="ready"):
        return store.board.file_ticket(self.conn, self.project_id, "NAT", body,
                                       column=column)

    def draft(self, *words):
        return holo("draft", *words, "-p", str(self.target))


class SkeletonTests(DraftCase):
    TEMPLATE = (ROOT / "holophyte/tickets/templates/ticketTemplate.md").read_text()

    def test_the_skeleton_names_the_profiles_as_the_headers_last_line(self):
        self.project_with(PROFILES)
        code, out, err = self.draft()
        self.assertEqual(code, 0, err)
        before, after = self.TEMPLATE.split("\n+++\n# {{TITLE}}\n")
        self.assertEqual(out, before
                         + "\n# this project's implementer profiles: fast, deep"
                         + "\n+++\n# {{TITLE}}\n" + after)

    def test_without_profiles_the_skeleton_is_the_template_byte_for_byte(self):
        self.project_with("")
        code, out, err = self.draft()
        self.assertEqual((code, out), (0, self.TEMPLATE), err)


class ConversionTests(DraftCase):
    def test_a_magic_line_ticket_prints_its_fields_in_a_header_above_its_body(self):
        self.project_with(PROFILES)
        self.assertEqual(self.filed(MAGIC), "NAT-1")
        code, out, err = self.draft("NAT-1")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, (
            '+++\nui_change = "minor"\nblast_radius = "medium"\nestimate = 30\n'
            'depends_on = []\nimplementer = "fast"\n+++\n'
            + TOP + "\n\n" + MIDDLE + NOTES + OPEN))
        filed, drafted = ticket_template.parse(MAGIC), ticket_template.parse(out)
        for field in ("ui_change", "blast_radius", "estimate_min", "depends_on",
                      "implementer"):
            self.assertEqual(getattr(drafted, field), getattr(filed, field), field)
        self.assertEqual(drafted.header_problems, [])

    def test_mixed_case_field_values_convert_to_valid_header_values(self):
        self.project_with(PROFILES)
        key = self.filed(MAGIC.replace("** minor", "** Minor")
                         .replace("** medium", "** Medium"))
        code, out, err = self.draft(key)
        self.assertEqual(code, 0, err)
        self.assertIn('+++\nui_change = "minor"\nblast_radius = "medium"\n', out)
        self.assertEqual(ticket_template.parse(out).header_problems, [])

    def test_mockup_and_evidence_sections_become_their_header_tables(self):
        body = TOP + MOCKUP + MIDDLE + EVIDENCE + NOTES + ESTIMATE + OPEN
        out = ticket_header.converted(body)
        self.assertEqual(out.split("+++\n")[1], """\
estimate = 30
depends_on = []
implementer = "fast"
[mockup]
url = "https://claude.ai/artifact/abc123"
approved = 2026-10-19
note = "the defaults"
[[mockup.boards]]
url = "https://media.example/row-dawn.png"
caption = "Row, Dawn"
[[mockup.boards]]
url = "https://media.example/row.zip"
[[evidence]]
file = "01-row-dawn.png"
state = "the row in Dawn"
[[evidence]]
file = "02-the-row-in.png"
state = "The row in Night!"
[[evidence]]
file = "03-flow.webm"
state = "the send-back flow"
""")
        self.assertNotIn("## Mock-up", out)
        self.assertNotIn("## Evidence", out)
        self.assertTrue(out.endswith("+++\n" + TOP + MIDDLE + NOTES + OPEN))

    def test_a_state_without_ascii_words_gets_a_file_name_the_header_accepts(self):
        body = TOP + "## Evidence\n\n- 登录页面\n- —\n\n" + NOTES + ESTIMATE + OPEN
        drafted = ticket_template.parse(ticket_header.converted(body))
        self.assertEqual(drafted.header_problems, [])
        self.assertEqual(drafted.evidence_states, ["登录页面", "—"])

    def test_a_fenced_field_example_stays_in_the_body_beside_the_real_field(self):
        example = "```\n**UI change:** major\n```\n\n"
        out = ticket_header.converted(TOP + example + FIELD_LINES + MIDDLE + NOTES
                                      + ESTIMATE + OPEN)
        self.assertIn('ui_change = "minor"\n', out)
        self.assertIn(TOP + example + "\n\n" + MIDDLE, out)
        self.assertEqual(ticket_template.parse(out).header_problems, [])


class RefusalTests(DraftCase):
    def test_a_mockup_line_no_header_field_holds_exits_1_naming_it(self):
        self.project_with(PROFILES)
        stray = MOCKUP.replace("https://media.example/row.zip\n",
                               "https://media.example/row.zip\n\n"
                               "See the second board for spacing.\n")
        key = self.filed(TOP + stray + MIDDLE + NOTES + ESTIMATE + OPEN,
                         column="backlog")
        code, out, err = self.draft(key)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("See the second board for spacing.", err)

    def test_a_header_form_ticket_prints_unchanged(self):
        self.project_with(PROFILES)
        body = ticket_header.converted(MAGIC)
        key = self.filed(body)
        code, out, err = self.draft(key)
        self.assertEqual((code, out), (0, body), err)

    def test_an_unknown_key_exits_1_naming_it(self):
        self.project_with("")
        code, out, err = self.draft("NAT-9")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("NAT-9", err)


if __name__ == "__main__":
    unittest.main()
