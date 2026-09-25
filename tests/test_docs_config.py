"""Keep the operator reference aligned with the accepted configuration keys."""
import re
import unittest
from pathlib import Path

from holophyte.config import (
    AGENT_CONFIG_KEYS,
    KNOWN_KEYS,
    REVIEW_ROUTE_KEYS,
    SERVE_KEYS,
)
from holophyte.config_tables import (
    AGENT_FALLBACK_KEYS,
    BOARD_KEYS,
    BOARD_MODE_KEYS,
    CONSOLE_KEYS,
    LOOP_KEYS,
    MERGE_KEYS,
    REPORT_KEYS,
    SUPERVISOR_KEYS,
)

DOCS = Path(__file__).resolve().parents[1] / "docs"
TABLES = {
    **KNOWN_KEYS,
    "agents": (set(AGENT_CONFIG_KEYS.values()) | set(AGENT_FALLBACK_KEYS)
               | set(REVIEW_ROUTE_KEYS) | set(KNOWN_KEYS["agents"])),
    "loop": LOOP_KEYS,
    "board": set(BOARD_KEYS) | set(BOARD_MODE_KEYS),
    "merge": MERGE_KEYS,
    "supervisor": SUPERVISOR_KEYS,
    "serve": SERVE_KEYS,
    "console": CONSOLE_KEYS,
    "report": REPORT_KEYS,
    # The nested bucket validator has no exported key table.
    "merge.media_bucket": {"endpoint", "bucket", "public_base", "retention_days"},
}


def sections(document):
    parts = re.split(r"^## `\[([^\]]+)\]`\s*$", document, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2]))


def entries(section):
    """Only an entry heading or first table cell counts, not a passing mention."""
    return re.findall(r"^(?:### |\| )`([^`]+)`", section, re.MULTILINE)


def assert_documented(case, document):
    by_table = sections(document)
    for table, keys in TABLES.items():
        case.assertIn(table, by_table, f"missing section [{table}]")
        documented = entries(by_table[table])
        for key in keys:
            case.assertIn(key, documented, f"[{table}] missing entry `{key}`")


class ConfigReferenceTests(unittest.TestCase):
    def test_reference_covers_code_tables_and_defaults(self):
        document = (DOCS / "config.md").read_text()
        assert_documented(self, document)
        headings = re.findall(r"^## (.+)$", document, re.MULTILINE)
        self.assertCountEqual(headings, [f"`[{table}]`" for table in TABLES])
        by_table = sections(document)
        self.assertIn("default", by_table["verify"].lower())
        for table, keys in TABLES.items():
            for key in keys:
                with self.subTest(table=table, key=key):
                    row = re.search(rf"^\| `{re.escape(key)}` \|.*$",
                                    by_table[table], re.MULTILINE)
                    self.assertIsNotNone(row, f"[{table}] needs a key row: {key}")
                    self.assertIn("default", row.group().lower())

    def test_removed_entry_names_missing_key_even_if_mentioned_elsewhere(self):
        document = (DOCS / "config.md").read_text()
        removed, count = re.subn(r"^\| `media_repo` \|.*\n", "", document,
                                flags=re.MULTILINE)
        self.assertEqual(count, 1)
        with self.assertRaisesRegex(
                AssertionError, r"\[merge\] missing entry `media_repo`"):
            assert_documented(self, removed)
        misplaced = removed + "\n| `media_repo` | default empty |\n"
        with self.assertRaisesRegex(
                AssertionError, r"\[merge\] missing entry `media_repo`"):
            assert_documented(self, misplaced)

    def test_review_session_wrapper_contract(self):
        document = (DOCS / "config.md").read_text()
        for term in ("review_session", "HOLOPHYTE_REVIEW_RESUME",
                     "HOLOPHYTE_REVIEW_SCRATCH", "200", "whitespace"):
            self.assertIn(term, document)

    def test_review_behavior_notes(self):
        document = (DOCS / "reviewing.md").read_text().lower()
        for term in ("conversation", "explicit", "rewritten"):
            with self.subTest(term=term):
                self.assertIn(term, document)

    def test_covering_review_scope_notes(self):
        document = (DOCS / "reviewing.md").read_text()
        boundary = document.split("## Local reviewer boundary", 1)[1]
        boundary = boundary.split("\n## ", 1)[0]
        step = document.split("6. **Review the fix.**", 1)[1]
        step = step.split("After `[merge] pr_rounds`", 1)[0]
        for term in ("approval at", "void", "read whole"):
            with self.subTest(term=term):
                self.assertIn(term, step)
        self.assertIn("focused", boundary)
