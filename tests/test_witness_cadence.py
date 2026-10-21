"""A loop pass runs a story's whole witness set only when it is due: when a
child's merge, a change under the story's witness paths or the nightly hour
came since the set's last real run, or when it has none. Otherwise the pass
carries the last real verdicts to the new commit; a hand-run pass runs it.

Run: python3 -m unittest discover -s tests -p 'test_witness_cadence.py' -v
"""
import contextlib
import datetime
import io
import os
import shlex
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402
from story_fixture import write_story  # noqa: E402

import holophyte.cli.entry  # noqa: E402
import linear_provider  # noqa: E402
import store.stories  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from holophyte.story.story_close import rerun_owed  # noqa: E402
from holophyte.story.witness import pass_pending, witness_pass  # noqa: E402
from store.stories import (  # noqa: E402
    accept_witness,
    record_witness_result,
    witness_ledger,
)
from tests.test_cli_native_update import NATIVE, no_linear  # noqa: E402
from tests.test_story_template import paths_section  # noqa: E402
from tests.test_witness_runner import (  # noqa: E402
    FAILS_AN_ASSERTION,
    PASSES,
    PYTHON,
    WitnessRunnerFixture,
    commit_file,
)

HOUR_MS = 3600 * 1000
LANDED = PASSES + "\n# landed on main\n"
FIXTURE_DIR = "lib/reader/"


class WitnessPathsFilingTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.locate(NATIVE)
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        self.directory = write_story(
            self.project.holo_dir / "stories", name="orders-csv",
            extra=paths_section("lib/reader/", "app/contract/"))

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                status = holophyte.cli.entry.cli([str(self.target), *args]) or 0
            except SystemExit as exited:
                status = exited.code
        return status, out.getvalue().splitlines()

    def stored(self):
        with contextlib.closing(open_store(self.project)) as conn:
            (parent_id, revision) = conn.execute(
                "SELECT id, revision FROM tickets"
                " WHERE linearIdentifier = 'NAT-1'").fetchone()
            return store.stories.story(conn, parent_id), revision

    def test_filed_witness_paths_are_read_back_and_a_change_needs_approval(self):
        status, lines = self.cli("--file-story", "orders-csv")
        self.assertEqual(status, 0, lines)
        found, revision = self.stored()
        with contextlib.closing(open_store(self.project)) as conn:
            store.stories.approve_story(conn, found.ticketId, revision,
                                        "maintainer", "reads right")
        found, revision = self.stored()
        self.assertEqual((found.state, found.witnessPaths),
                         ("approved", ("lib/reader/", "app/contract/")))
        body = self.directory / "story.md"
        body.write_text(body.read_text().replace("- app/contract/\n", ""))

        status, lines = self.cli("--file-story", "orders-csv", "--update",
                                 "NAT-1", "--revision", str(revision))

        self.assertEqual(status, 0, lines)
        found, _ = self.stored()
        self.assertEqual((found.state, found.witnessPaths),
                         ("planned", ("lib/reader/",)))


class WitnessSetPassTests(WitnessRunnerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.target = self.project()

    def counter(self, key):
        path = self.root / f"{key}-runs"
        return path.read_text().count("\n") if path.exists() else 0

    def command(self, key):
        counter = shlex.quote(str(self.root / f"{key}-runs"))
        return (f"echo ran >> {counter} &&"
                f" {PYTHON} -m unittest tests.test_{key.lower()}")

    def story(self, witness_paths=(), source=PASSES):
        self.approve([(key, f"tests/test_{key.lower()}.py", self.command(key),
                       source) for key in ("W1", "W2")],
                     witness_paths=witness_paths)

    def land(self):
        for key in ("W1", "W2"):
            commit_file(self.repo, f"tests/test_{key.lower()}.py", LANDED,
                        f"{key} lands")
        sha = commit_file(self.repo, f"{FIXTURE_DIR}page.txt", "one\n",
                          "fixture")
        self.loop()
        return sha

    def loop(self):
        return witness_pass(self.target, self.conn, self.story_id, "loop")

    def age_real_rows(self, ms):
        self.conn.execute("UPDATE witnessResults SET at = ?"
                          " WHERE carriedFrom IS NULL",
                          (int(time.time() * 1000) - ms,))
        self.conn.commit()

    def at(self, sha):
        return {row.witnessKey: (row.verdict, row.carriedFrom, row.seconds)
                for row in witness_ledger(self.conn, self.story_id)
                if row.mainSha == sha}

    def assert_ran(self, sha, runs):
        self.assertEqual({key: carried for key, (_, carried, _)
                          in self.at(sha).items()}, {"W1": None, "W2": None})
        self.assertEqual((self.counter("W1"), self.counter("W2")), (runs, runs))

    def assert_carried(self, sha, source, runs):
        self.assertEqual(self.at(sha), {"W1": ("green", source, 0),
                                        "W2": ("green", source, 0)})
        self.assertEqual((self.counter("W1"), self.counter("W2")), (runs, runs))

    def test_a_commit_with_no_trigger_carries_the_whole_set(self):
        self.story()
        a = self.land()
        self.assert_ran(a, 1)

        b = commit_file(self.repo, "README.md", "more\n", "docs")
        self.loop()

        self.assert_carried(b, a, 1)

    def test_a_child_merge_since_the_last_real_run_runs_the_set(self):
        self.story()
        self.land()
        c = commit_file(self.repo, "src/export.py", "x = 1\n", "child work")
        self.child_merged(c)

        self.loop()
        self.assert_ran(c, 2)

        d = commit_file(self.repo, "README.md", "more\n", "docs")
        self.loop()
        self.assert_carried(d, c, 2)

    def test_a_change_under_the_witness_paths_runs_the_set(self):
        self.story(witness_paths=[FIXTURE_DIR])
        a = self.land()
        b = commit_file(self.repo, "README.md", "more\n", "docs")
        self.loop()
        self.assert_carried(b, a, 1)

        c = commit_file(self.repo, f"{FIXTURE_DIR}page.txt", "two\n", "page")
        self.loop()

        self.assert_ran(c, 2)

    def test_a_last_run_before_the_nightly_hour_is_due_even_at_a_carried_tip(self):
        hour = (datetime.datetime.now().hour + 12) % 24
        self.target = self.project(f"[story]\nwitness_nightly_hour = {hour}\n")
        self.story()
        a = self.land()
        self.age_real_rows(HOUR_MS)
        b = commit_file(self.repo, "README.md", "more\n", "docs")
        self.loop()
        self.assert_carried(b, a, 1)
        self.assertEqual(pass_pending(self.target, self.conn, 1), [])

        self.age_real_rows(14 * HOUR_MS)

        self.assertEqual(pass_pending(self.target, self.conn, 1),
                         [self.story_id])
        self.loop()
        self.assert_ran(b, 2)
        self.assertEqual(pass_pending(self.target, self.conn, 1), [])

    def test_a_last_run_before_the_nightly_hour_runs_again_at_the_same_tip(self):
        hour = (datetime.datetime.now().hour + 12) % 24
        self.target = self.project(f"[story]\nwitness_nightly_hour = {hour}\n")
        self.story()
        a = self.land()
        self.assertEqual(pass_pending(self.target, self.conn, 1), [])

        self.age_real_rows(14 * HOUR_MS)

        self.assertEqual(pass_pending(self.target, self.conn, 1),
                         [self.story_id])
        rows = self.loop()
        self.assertEqual([(row.witnessKey, row.mainSha, row.carriedFrom)
                          for row in rows], [("W1", a, None), ("W2", a, None)])
        self.assertEqual((self.counter("W1"), self.counter("W2")), (2, 2))
        self.assertEqual(pass_pending(self.target, self.conn, 1), [])

    def test_a_pass_cut_short_after_one_witness_runs_the_rest_on_retry(self):
        self.story()
        self.land()
        c = commit_file(self.repo, "src/export.py", "x = 1\n", "child work")
        self.child_merged(c)
        record_witness_result(self.conn, self.story_id, "W1", c, "green",
                              "loop")

        self.loop()

        (w2,) = [row for row in witness_ledger(self.conn, self.story_id)
                 if row.mainSha == c and row.witnessKey == "W2"]
        self.assertIsNone(w2.carriedFrom)
        self.assertEqual(self.counter("W2"), 2)

    def test_a_failing_git_command_counts_as_due(self):
        self.story(witness_paths=[FIXTURE_DIR])
        self.land()
        self.conn.execute("UPDATE witnessResults SET mainSha = ?",
                          ("f" * 40,))
        self.conn.commit()

        b = commit_file(self.repo, "README.md", "more\n", "docs")
        self.loop()

        self.assert_ran(b, 2)

    def test_a_carried_red_owes_no_rerun_and_writes_no_note(self):
        self.story()
        self.land()
        b = commit_file(self.repo, "tests/test_w2.py", FAILS_AN_ASSERTION,
                        "w2 breaks")
        self.child_merged(b)
        self.loop()
        self.assertEqual([row.verdict for row in witness_ledger(
            self.conn, self.story_id) if row.mainSha == b
            and row.witnessKey == "W2"], ["red", "red"])

        c = commit_file(self.repo, "README.md", "more\n", "docs")
        self.loop()
        self.loop()

        w2 = [row for row in witness_ledger(self.conn, self.story_id)
              if row.mainSha == c and row.witnessKey == "W2"]
        self.assertEqual([(row.verdict, row.carriedFrom) for row in w2],
                         [("red", b)])
        self.assertEqual(rerun_owed(self.conn, self.story_id, c), set())
        notes = dict(self.conn.execute(
            "SELECT dedupKey, COUNT(*) FROM ticketNotes WHERE kind = 'verdict'"
            " GROUP BY dedupKey").fetchall())
        self.assertEqual(notes.get(f"witness:{self.story_id}:W2:{b}"), 1)
        self.assertNotIn(f"witness:{self.story_id}:W2:{c}", notes)

    def test_a_red_run_over_a_carried_green_is_still_owed_its_rerun(self):
        self.story()
        a = self.land()
        b = commit_file(self.repo, "README.md", "more\n", "docs")
        self.loop()
        self.assert_carried(b, a, 1)

        record_witness_result(self.conn, self.story_id, "W1", b, "red", "loop",
                              red_kind="assert")

        self.assertEqual(rerun_owed(self.conn, self.story_id, b), {"W1"})

    def test_a_story_closes_on_carried_greens_and_its_note_says_so(self):
        self.story(source=LANDED + "# approved\n")
        a = self.land()
        self.assertEqual(store.stories.story(self.conn, self.story_id).state,
                         "approved")
        for key in ("W1", "W2"):
            accept_witness(self.conn, self.story_id, key, LANDED)

        b = commit_file(self.repo, "README.md", "more\n", "docs")
        self.loop()

        self.assert_carried(b, a, 1)
        found = store.stories.story(self.conn, self.story_id)
        self.assertEqual((found.state, found.closedSha), ("closed", b))
        (note,) = self.conn.execute(
            "SELECT text FROM ticketNotes WHERE kind = 'verdict'"
            " AND dedupKey = ?",
            (f"story-closed:{self.story_id}:{b}",)).fetchone()
        self.assertIn(f"W2 green (carried from {a[:12]})", note)

    def test_a_hand_run_pass_runs_a_set_that_is_not_due(self):
        self.story()
        self.land()
        b = commit_file(self.repo, "README.md", "more\n", "docs")

        rows = witness_pass(self.target, self.conn, self.story_id, "operator")

        self.assertEqual([(row.witnessKey, row.carriedFrom, row.verifier)
                          for row in rows],
                         [("W1", None, "operator"), ("W2", None, "operator")])
        self.assertEqual(self.at(b)["W2"][:2], ("green", None))
        self.assertEqual((self.counter("W1"), self.counter("W2")), (2, 2))


if __name__ == "__main__":
    unittest.main()
