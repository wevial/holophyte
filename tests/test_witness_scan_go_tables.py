import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from holophyte.review.reply_parsing import missing_witnesses

TEST_FILE = "internal/triage_test.go"

STRING_SLICE = """package triage

import "testing"

func TestTriage(t *testing.T) {
	for _, tool := range []string{"mark_read", "mark_unread"} {
		t.Run(tool, func(t *testing.T) {
			t.Run("missing-uid-mixed", func(t *testing.T) {})
		})
	}
}
"""

POSITIONAL_STRUCT = """package triage

import "testing"

func TestTriage(t *testing.T) {
	for _, setting := range []struct {
		name  string
		value *bool
	}{
		{"default-off", nil},
	} {
		t.Run(setting.name, func(t *testing.T) {})
	}
}
"""

KEYED_STRUCT = """package triage

import "testing"

func TestTriage(t *testing.T) {
	cases := []struct{ name string }{
		{name: "explicit-disabled"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {})
	}
}
"""

COMPUTED_RUN = """package triage

import "testing"

func TestTriage(t *testing.T) {
	for _, tool := range []string{%s} {
		t.Run(tool, func(t *testing.T) {})
	}
}
"""

ELSEWHERE = COMPUTED_RUN % '"mark_read"' + """
func mailboxFor(tool string) string {
	return "archive_mail"
}
"""

LITERAL_ONLY = """package triage

import "testing"

func TestTriage(t *testing.T) {
	for _, tool := range []string{"mark_read"} {
		t.Run(tool, func(t *testing.T) {})
	}
}

func TestPlain(t *testing.T) {
	mailbox := "archive_mail"
	t.Run("uses-mailbox", func(t *testing.T) { _ = mailbox })
}
"""

NESTED_TABLES = """package triage

import "testing"

func TestTables(t *testing.T) {
	for _, tool := range []string{"mark_read", "mark_unread"} {
		t.Run(tool, func(t *testing.T) {
			t.Run("missing-uid-mixed", func(t *testing.T) {})
		})
	}
	for _, outer := range []struct {
		name  string
		inner []struct{ name string }
	}{
		{"archive mail", []struct{ name string }{{"to-label"}, {name: "to trash"}}},
		{name: "move", inner: []struct{ name string }{{"destination-label"}}},
	} {
		t.Run(outer.name, func(t *testing.T) {
			for _, inner := range outer.inner {
				t.Run(inner.name, func(t *testing.T) {})
			}
		})
	}
}
"""


def missing_in(root, source, name):
    test = Path(root) / TEST_FILE
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text(source)
    return missing_witnesses([(TEST_FILE, None, name)], root)


class GoTableSubtestWitnessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def test_string_slice_title_is_found_with_its_literal_child(self):
        name = "TestTriage/mark_read/missing-uid-mixed"
        self.assertEqual(missing_in(self.root, STRING_SLICE, name), [])

    def test_struct_table_titles_are_found_positional_and_keyed(self):
        for source, name in ((POSITIONAL_STRUCT, "TestTriage/default-off"),
                             (KEYED_STRUCT, "TestTriage/explicit-disabled")):
            with self.subTest(name=name):
                self.assertEqual(missing_in(self.root, source, name), [])

    def test_segment_not_held_whole_in_a_computing_parent_is_missing(self):
        cases = {
            "nowhere in the file": (COMPUTED_RUN % '"mark_read"', "TestTriage"),
            "only in another function": (ELSEWHERE, "TestTriage"),
            "only inside a longer literal":
                (COMPUTED_RUN % '"archive_mail_all"', "TestTriage"),
            "in a parent with only literal titles": (LITERAL_ONLY, "TestPlain"),
        }
        for case, (source, parent) in cases.items():
            with self.subTest(case=case):
                name = f"{parent}/archive_mail"
                (note,) = missing_in(self.root, source, name)
                self.assertIn(f"{TEST_FILE}::{name}", note)
                self.assertIn('no .Run("archive_mail")', note)


class GoTestReportedSubtestsTests(unittest.TestCase):
    def test_every_subtest_go_test_passes_is_found(self):
        go = shutil.which("go")
        if go is None:
            self.skipTest("go is not installed")
        with tempfile.TemporaryDirectory() as root:
            Path(root, "go.mod").write_text("module example.com/triage\n\ngo 1.21\n")
            test = Path(root, TEST_FILE)
            test.parent.mkdir(parents=True)
            test.write_text(NESTED_TABLES)
            env = {**os.environ, "GOTOOLCHAIN": "local", "GOFLAGS": "",
                   "GOCACHE": str(Path(root, ".gocache"))}
            run = subprocess.run([go, "test", "-v", "-count=1", "./..."], cwd=root,
                                 env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            names = re.findall(r"--- PASS: (\S+/\S+) ", run.stdout)
            self.assertGreaterEqual(len(names), 9, run.stdout)
            self.assertIn("TestTables/archive_mail/to_trash", names)
            references = [(TEST_FILE, None, name) for name in names]
            self.assertEqual(missing_witnesses(references, root), [])


if __name__ == "__main__":
    unittest.main()
