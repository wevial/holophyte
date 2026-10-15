"""The landmark check parks a ticket only for a landmark main has lost.

Run: python3 -m unittest discover -s tests -p 'test_freshness_landmarks.py' -v
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit  # noqa: E402 - after the sys.path insert
from loop_fixture import (  # noqa: E402 - after the sys.path insert
    VALID_BODY,
    LoopFixture,
    StubProvider,
    a_task,
)

from holophyte.review.freshness import landmark_reasons  # noqa: E402

IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def commit_main(repo, files):
    git(repo, "init", "-q", "-b", "main")
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, *IDENTITY, "commit", "-q", "-m", "fixture")


def notes_body(item):
    return VALID_BODY.replace("## Implementation notes\n\n* None.\n",
                              f"## Implementation notes\n\n* {item}\n")


def criteria_body(*criteria):
    boxes = "".join(f"- [ ] {c} (a test witnesses this)\n" for c in criteria)
    return VALID_BODY.replace(
        "- [ ] Given the thing, when it runs, then it works (a test witnesses"
        " this)\n", boxes)


class RepoCase(unittest.TestCase):
    FILES = {}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        commit_main(self.repo, self.FILES)

    def findings(self, body):
        return landmark_reasons(self.repo, body)


class LibraryNameTests(RepoCase):
    FILES = {
        "holophyte/serve/views.py": "BACKLOG = 'backlog'\n",
        "holophyte/serve/contract.py": "from typing import Literal\n",
    }

    def test_a_library_name_found_elsewhere_on_main_is_no_stale_reason(self):
        body = notes_body("A column's `state` is a `Literal` of `BACKLOG` in"
                          " `holophyte/serve/views.py`.")
        reasons, _ = self.findings(body)
        self.assertFalse([r for r in reasons if "`Literal`" in r], reasons)


class SymbolPairingTests(RepoCase):
    FILES = {
        "holophyte/serve/serve_runs.py": ("def chain_figures():\n    pass\n"
                                          "def shipped():\n    pass\n"),
        "console/src/lib/types.ts": "export interface Run {}\n",
        "console/src/lib/derive.ts": "export const x = 1;\n",
        "console/src/lib/runs.ts": "export function phaseLabel() {}\n",
        "holophyte/loop/claim.py": "def admit():\n    pass\n",
        "console/e2e/swap.spec.ts": "test('swap', () => {});\n",
        "src/lotuspod/media.py": "def load():\n    pass\n",
    }

    def test_an_unpaired_symbol_defined_elsewhere_on_main_is_nothing(self):
        body = notes_body("`shipped()` rows spread `chain_figures()`. Read that"
                          " function rather than the hand interface in"
                          " `console/src/lib/types.ts`.")
        self.assertEqual(self.findings(body), ([], []))

    def test_a_paired_symbol_found_in_another_file_is_an_advisory(self):
        body = notes_body("Reuse `phaseLabel()` from"
                          " `console/src/lib/derive.ts`.")
        stale, advisories = self.findings(body)
        self.assertEqual(stale, [])
        self.assertEqual(len(advisories), 1, advisories)
        self.assertIn("`console/src/lib/runs.ts`", advisories[0])

    def test_a_symbol_found_nowhere_keeps_the_stale_wording(self):
        body = notes_body("Call `gone_helper()` in `holophyte/loop/claim.py`.")
        self.assertEqual(self.findings(body), ([
            "`gone_helper()` (named in Implementation notes #1) is not in"
            " `holophyte/loop/claim.py` on main"], []))

    def test_a_symbol_paired_with_a_directory_and_found_nowhere_is_stale(self):
        body = notes_body("Add `GoneThing` in `holophyte/loop/` and"
                          " `holophyte/loop/claim.py`.")
        stale, _ = self.findings(body)
        self.assertEqual(len(stale), 1, stale)
        self.assertIn("`GoneThing`", stale[0])

    def test_a_call_on_a_library_receiver_is_not_checked(self):
        body = notes_body("Stub the API with `route.fallback()` in"
                          " `console/e2e/swap.spec.ts`.")
        self.assertEqual(self.findings(body), ([], []))

    def test_a_call_on_a_repo_module_receiver_is_checked(self):
        body = notes_body("Save it with `media.store()` in"
                          " `src/lotuspod/media.py`.")
        stale, advisories = self.findings(body)
        self.assertEqual(len(stale), 1, stale)
        self.assertIn("`media.store()`", stale[0])
        self.assertEqual(advisories, [])


class PathWordTests(RepoCase):
    FILES = {
        "holophyte/loop/gates.py": "GATES = []\n",
        "src/lotuspod/_theme/js/app.js": "1;\n",
        "docs/caf\u00e9.md": "menu\n",
    }

    def test_a_non_ascii_path_on_main_is_not_stale(self):
        body = criteria_body("Given `docs/caf\u00e9.md`, then it reads")
        self.assertEqual(self.findings(body), ([], []))

    def test_a_path_followed_by_a_new_declaration_is_not_stale(self):
        body = criteria_body(
            "Given `src/lotuspod/_theme/js/calltree.js` (a new file, built"
            " from it), then it folds",
            "Given `web/src/page-swap.ts` (new), then it swaps")
        self.assertEqual(self.findings(body), ([], []))

    def test_new_inside_other_parentheses_declares_nothing(self):
        body = criteria_body(
            "Given `web/src/other.ts` (see the new build), then it works")
        stale, _ = self.findings(body)
        self.assertEqual(stale, ["`web/src/other.ts` (named in Acceptance"
                                 " criteria #1) is not on main"])

    def test_placeholders_and_path_shaped_words_are_not_paths(self):
        body = criteria_body(*(f"Given `{word}`, then it works" for word in (
            "shadow/SLUG", "n/a", "contractReader/extract", "NAME.html",
            "DIR/_headers")))
        self.assertEqual(self.findings(body), ([], []))

    def test_a_missing_file_under_an_existing_directory_is_stale(self):
        body = criteria_body("Given `holophyte/gone/thing.py`, then it works")
        stale, _ = self.findings(body)
        self.assertEqual(stale, ["`holophyte/gone/thing.py` (named in"
                                 " Acceptance criteria #1) is not on main"])

    def test_a_bare_basename_of_a_tracked_file_is_satisfied(self):
        self.assertEqual(self.findings(notes_body("Edit `gates.py`.")),
                         ([], []))

    def test_a_bare_basename_matching_nothing_is_an_advisory(self):
        stale, advisories = self.findings(notes_body("Edit `nowhere.py`."))
        self.assertEqual(stale, [])
        self.assertEqual(len(advisories), 1, advisories)
        self.assertIn("`nowhere.py`", advisories[0])


class ClaimAdvisoryTests(LoopFixture):
    def test_an_advisory_is_a_run_warning_and_never_parks(self):
        body = notes_body("Edit `nowhere.py` for the thing.")
        provider = StubProvider(dict(a_task(1), body=body))

        self.loop(Commit("the thing"), APPROVE, provider=provider)

        warnings = [summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'warning'")]
        self.assertTrue([w for w in warnings if "`nowhere.py`" in w], warnings)
        self.assertNotEqual(self.read(
            "SELECT status FROM tickets WHERE linearIdentifier = 'KO-131'"),
            [("needs_spec",)])


if __name__ == "__main__":
    unittest.main()
