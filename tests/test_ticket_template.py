"""Tests for ticket_template: parse + validate ticketTemplate.md tickets.

Run: python3 -m unittest discover tests -v
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ticket_template as tt

ROOT = Path(__file__).resolve().parent.parent

FILLED = """\
# Add export endpoint

## Summary

Add a CSV export endpoint for the orders list.

## What / Why / How

**What:** GET /orders.csv streams the current user's orders as CSV.

**Why:** Ops needs orders in spreadsheets without database access.

**How:** Reuse the orders query service and stream via the csv module.

## In scope

- CSV serialization of the orders list

## Out of scope

- Excel-specific formatting

## Acceptance criteria

- [ ] Given 3 orders, when GET /orders.csv, then 4 lines including header.
- [ ] Given no orders, when GET /orders.csv, then only the header row.

## Verify command(s)

```
python3 -m unittest test_orders_export
```

## Implementation notes

- Endpoint lives beside the other order routes.

## Estimate & dependencies

Estimate: 30 min · Depends on: none

## Open questions

- None
"""

BLOCKQUOTE = """\
> Machine-checkable dependencies are Linear **blocks** relations — the loop
> enforces only those. Anything outside Linear (DNS, human review, hardware)
> gates via triage: the ticket stays in Backlog until resolved, then moves to
> Todo. Keep this line in sync with the relations for human readers.
"""

FILLED_WITH_BLOCKQUOTE = FILLED.replace(
    "Estimate: 30 min · Depends on: none\n",
    "Estimate: 30 min · Depends on: none\n\n" + BLOCKQUOTE)

CONTRACTS = """\
## Contract checks

```
config/tunnel.yml: 8622
```

"""

FILLED_WITH_CONTRACTS = FILLED.replace("## Implementation notes",
                                       CONTRACTS + "## Implementation notes")

# What Linear stores after normalizing an authored body: the bold run swallows
# the space after the colon, and "- " list markers come back as "* ".
LINEAR_NORMALIZED = (FILLED.replace("**What:**", "**What: **")
                           .replace("**Why:**", "**Why: **")
                           .replace("**How:**", "**How: **")
                           .replace("- None", "* None"))


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.t = tt.parse(FILLED)

    def test_title(self):
        self.assertEqual(self.t.title, "Add export endpoint")

    def test_sections_in_template_order(self):
        self.assertEqual(self.t.order, tt.SECTION_ORDER)

    def test_what_why_how(self):
        self.assertEqual(self.t.what,
                         "GET /orders.csv streams the current user's orders as CSV.")
        self.assertIn("spreadsheets", self.t.why)
        self.assertIn("csv module", self.t.how)

    def test_scope_bullets(self):
        self.assertEqual(self.t.in_scope,
                         ["CSV serialization of the orders list"])
        self.assertEqual(self.t.out_of_scope, ["Excel-specific formatting"])

    def test_acceptance_criteria(self):
        self.assertEqual(len(self.t.acceptance), 2)
        self.assertTrue(self.t.acceptance[0].startswith("Given 3 orders"))

    def test_verify_commands_from_fence(self):
        self.assertEqual(self.t.verify_commands,
                         ["python3 -m unittest test_orders_export"])

    def test_estimate_and_dependencies(self):
        self.assertEqual(self.t.estimate_min, 30)
        self.assertEqual(self.t.depends_on, [])

    def test_open_questions_none_with_trailing_comment(self):
        self.assertTrue(self.t.open_questions_none)

    def test_depends_on_ids_split(self):
        t = tt.parse(FILLED.replace(
            "Depends on: none", "Depends on: KO-1, KO-23"))
        self.assertEqual(t.depends_on, ["KO-1", "KO-23"])

    def test_estimate_line_ignores_trailing_blockquote(self):
        t = tt.parse(FILLED_WITH_BLOCKQUOTE)
        self.assertEqual(t.estimate_min, 30)
        self.assertEqual(t.depends_on, [])

    def test_no_contract_checks_section_means_no_declarations(self):
        self.assertEqual(self.t.contract_checks, [])

    def test_contract_checks_parsed_as_path_and_literal(self):
        t = tt.parse(FILLED_WITH_CONTRACTS)
        self.assertEqual(t.contract_checks, [("config/tunnel.yml", "8622")])

    def test_depends_on_ids_split_with_blockquote(self):
        t = tt.parse(FILLED_WITH_BLOCKQUOTE.replace(
            "Depends on: none", "Depends on: KO-1, KO-23"))
        self.assertEqual(t.depends_on, ["KO-1", "KO-23"])


class ValidateTests(unittest.TestCase):
    def assert_problems_contain(self, text, fragment):
        problems = tt.validate(tt.parse(text))
        self.assertTrue(any(fragment in p for p in problems),
                        f"{fragment!r} not in {problems}")

    def test_filled_ticket_is_valid(self):
        self.assertEqual(tt.validate(tt.parse(FILLED)), [])

    def test_filled_ticket_with_blockquote_is_valid(self):
        self.assertEqual(tt.validate(tt.parse(FILLED_WITH_BLOCKQUOTE)), [])

    def test_ticket_with_contract_checks_is_valid(self):
        self.assertEqual(tt.validate(tt.parse(FILLED_WITH_CONTRACTS)), [])

    def test_absolute_contract_path_rejected(self):
        for bad in ("/etc/tunnel.yml: 8622", "../outside/tunnel.yml: 8622"):
            self.assert_problems_contain(
                FILLED_WITH_CONTRACTS.replace("config/tunnel.yml: 8622", bad),
                "contract check path must")

    def test_contract_check_without_a_literal_rejected(self):
        self.assert_problems_contain(
            FILLED_WITH_CONTRACTS.replace("config/tunnel.yml: 8622",
                                          "config/tunnel.yml:"),
            "empty expected literal")

    def test_contract_checks_section_with_an_empty_fence_rejected(self):
        self.assert_problems_contain(
            FILLED_WITH_CONTRACTS.replace("config/tunnel.yml: 8622\n", ""),
            "no 'relative/path: expected literal' declarations")

    def test_linear_normalized_body_is_valid(self):
        self.assertEqual(tt.validate(tt.parse(LINEAR_NORMALIZED)), [])

    def test_linear_normalized_body_still_needs_its_fields(self):
        self.assert_problems_contain(
            LINEAR_NORMALIZED.replace(
                "**What: ** GET /orders.csv streams the current user's "
                "orders as CSV.\n", ""),
            "'**What:**' line missing")

    def test_linear_normalized_open_questions_still_must_be_none(self):
        self.assert_problems_contain(
            LINEAR_NORMALIZED.replace("* None", "* Which CSV dialect?"),
            "'- None'")

    def test_loose_what_line_is_still_rejected(self):
        self.assert_problems_contain(
            FILLED.replace("**What:** GET", "What: GET"),
            "'**What:**' line missing")

    def test_missing_section(self):
        self.assert_problems_contain(
            FILLED.replace("## Out of scope\n\n- Excel-specific formatting\n", ""),
            "missing section '## Out of scope'")

    def test_duplicate_section(self):
        self.assert_problems_contain(
            FILLED + "\n## Summary\n\ntwice\n",
            "duplicate section '## Summary'")

    def test_unknown_section(self):
        self.assert_problems_contain(FILLED + "\n## Bonus\n\nx\n",
                                     "unknown section '## Bonus'")

    def test_no_acceptance_criteria(self):
        self.assert_problems_contain(
            FILLED.replace("- [ ] Given 3 orders", "- Given 3 orders")
                  .replace("- [ ] Given no orders", "- Given no orders"),
            "no '- [ ]")

    def test_all_criteria_checked_off(self):
        self.assert_problems_contain(
            FILLED.replace("- [ ] Given", "- [x] Given"),
            "already checked off")

    def test_empty_verify_fence(self):
        self.assert_problems_contain(
            FILLED.replace("python3 -m unittest test_orders_export\n", ""),
            "no runnable command lines")

    def test_absolute_path_in_verify_command(self):
        for bad in ("pytest /srv/repo/tests",
                    "cd ~/other && pytest",
                    "git -C C:\\repo status"):
            self.assert_problems_contain(
                FILLED.replace("python3 -m unittest test_orders_export", bad),
                "non-relative path")

    def test_relative_flag_argument_is_not_flagged(self):
        text = FILLED.replace("python3 -m unittest test_orders_export",
                              "ruff check . && python3 -m pytest tests/ -q")
        self.assertEqual(tt.validate(tt.parse(text)), [])

    def test_bad_estimate_line(self):
        self.assert_problems_contain(
            FILLED.replace("Estimate: 30 min · Depends on: none",
                           "Estimate: half a day"),
            "'Estimate & dependencies' must read")

    def test_bad_dependency_id(self):
        self.assert_problems_contain(
            FILLED.replace("Depends on: none", "Depends on: fix auth"),
            "not a ticket ID")

    def test_open_questions_not_none(self):
        self.assert_problems_contain(
            FILLED.replace("- None", "- Which CSV dialect?"),
            "'- None'")

    def test_unfilled_placeholders_fail(self):
        problems = tt.validate(tt.parse(
            FILLED.replace("Add a CSV export endpoint for the orders list.",
                           "<Describe the outcome in one or two sentences.>")))
        self.assertTrue(any("placeholder" in p for p in problems), problems)
        self.assertTrue(any("{{" in p for p in tt.validate(tt.parse(
            FILLED.replace("Add export endpoint", "{{TITLE}}")))), problems)

    def test_missing_h1(self):
        problems = tt.validate(tt.parse(FILLED.replace("# Add export endpoint\n", "")))
        self.assertTrue(any("H1 title" in p for p in problems), problems)

    def test_extra_h1_rejected(self):
        problems = tt.validate(tt.parse(FILLED + "\n# Another doc\n"))
        self.assertTrue(any("extra H1" in p for p in problems), problems)


class TemplateFileTests(unittest.TestCase):
    """The real ticketTemplate.md must parse; unfilled, it must NOT validate."""

    def setUp(self):
        self.text = (ROOT / "ticketTemplate.md").read_text()
        self.t = tt.parse(self.text)

    def test_all_sections_recognized_in_order(self):
        self.assertEqual([n for n in self.t.order], tt.TEMPLATE_ORDER)

    def test_rules_block_inside_fence_not_treated_as_commands(self):
        self.assertEqual(
            [c for c in self.t.verify_commands if c.startswith(("-", "Rules:"))],
            [])

    def test_unfilled_template_fails_validation_with_placeholders(self):
        problems = tt.validate(self.t)
        self.assertTrue(problems)
        self.assertTrue(any("placeholder" in p for p in problems), problems)


class CliTests(unittest.TestCase):
    def run_cli(self, *paths):
        return subprocess.run(
            [sys.executable, str(ROOT / "ticket_template.py"), *paths],
            capture_output=True, text=True)

    def test_valid_file_exits_zero(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md") as f:
            f.write(FILLED)
            f.flush()
            r = self.run_cli(f.name)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK", r.stdout)

    def test_invalid_file_exits_nonzero_and_lists_problems(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md") as f:
            f.write(FILLED.replace("## Summary", "## Summaries"))
            f.flush()
            r = self.run_cli(f.name)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("INVALID", r.stdout)
        self.assertIn("missing section", r.stdout)

    def test_real_template_file_exits_nonzero(self):
        r = self.run_cli(str(ROOT / "ticketTemplate.md"))
        self.assertNotEqual(r.returncode, 0)

    def test_no_args_usage_exit_two(self):
        self.assertEqual(self.run_cli().returncode, 2)


if __name__ == "__main__":
    unittest.main()


class SectionOrderTests(unittest.TestCase):
    """validate() enforces the docstring's 'required sections in order' claim."""

    def test_reordered_sections_fail(self):
        summary_block = (
            "## Summary\n"
            "\n"
            "Add a CSV export endpoint for the orders list.\n")
        # move Summary to the very end: violates 'before What / Why / How'
        head = FILLED.replace(summary_block, "", 1).rstrip("\n")
        reordered = head + "\n\n" + summary_block
        problems = tt.validate(tt.parse(reordered))
        self.assertTrue(any("out of template order" in p for p in problems),
                        f"expected order error, got {problems}")

    def test_conforming_order_passes(self):
        problems = tt.validate(tt.parse(FILLED))
        self.assertFalse([p for p in problems if "out of template order" in p])
