"""A witness with a `## Witness cadence` line runs in a loop pass only when
it is due: when files under its paths or its own witness file changed since
its latest real verdict, or when that verdict is its hours old. Otherwise the
pass carries that verdict to the new commit, and a hand-run pass runs it.

Run: python3 -m unittest discover -s tests -p 'test_witness_cadence.py' -v
"""
import contextlib
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
from holophyte.story.witness import witness_pass  # noqa: E402
from store.stories import witness_ledger  # noqa: E402
from tests.test_cli_native_update import NATIVE, no_linear  # noqa: E402
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


def cadence_section(line):
    return "\n".join(["## Witness cadence", "", "```", line, "```", "", ""])


class CadenceFilingTests(ConfigTestCase):
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
            extra=cadence_section(
                "W2: paths lib/reader/ app/contract/; every 6h"))

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

    def test_a_filed_cadence_is_read_back_and_a_changed_one_needs_approval(self):
        status, lines = self.cli("--file-story", "orders-csv")
        self.assertEqual(status, 0, lines)
        found, revision = self.stored()
        with contextlib.closing(open_store(self.project)) as conn:
            store.stories.approve_story(conn, found.ticketId, revision,
                                        "maintainer", "reads right")
        found, revision = self.stored()
        self.assertEqual(found.state, "approved")
        self.assertEqual({witness.key: witness.cadence
                          for witness in found.witnesses},
                         {"W1": None, "W2": {"paths": ["lib/reader/",
                                                       "app/contract/"],
                                             "hours": 6}})
        body = self.directory / "story.md"
        body.write_text(body.read_text().replace("every 6h", "every 12h"))

        status, lines = self.cli("--file-story", "orders-csv", "--update",
                                 "NAT-1", "--revision", str(revision))

        self.assertEqual(status, 0, lines)
        found, _ = self.stored()
        self.assertEqual(found.state, "planned")
        self.assertEqual(found.witnesses[1].cadence["hours"], 12)


class CadencePassTests(WitnessRunnerFixture, unittest.TestCase):
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

    def story(self, cadence, sources=(PASSES, PASSES)):
        self.approve([(key, f"tests/test_{key.lower()}.py", self.command(key),
                       source) for key, source in zip(("W1", "W2"), sources)],
                     cadences={"W2": cadence})

    def land(self, *keys, text=LANDED):
        for key in keys:
            commit_file(self.repo, f"tests/test_{key.lower()}.py", text,
                        f"{key} lands")
        return commit_file(self.repo, f"{FIXTURE_DIR}page.txt", "one\n",
                           "fixture")

    def loop(self):
        return witness_pass(self.target, self.conn, self.story_id, "loop")

    def rows(self, key, sha):
        return [row for row in witness_ledger(self.conn, self.story_id)
                if row.witnessKey == key and row.mainSha == sha]

    def test_a_commit_outside_its_paths_carries_its_verdict(self):
        self.story({"paths": [FIXTURE_DIR]})
        a = self.land("W1", "W2")
        self.loop()

        b = commit_file(self.repo, "README.md", "more\n", "docs")
        self.loop()

        (w1,) = self.rows("W1", b)
        self.assertIsNone(w1.carriedFrom)
        self.assertEqual(self.counter("W1"), 2)
        (w2,) = self.rows("W2", b)
        self.assertEqual((w2.verdict, w2.carriedFrom, w2.seconds),
                         ("green", a, 0))
        self.assertEqual(self.counter("W2"), 1)

    def test_a_change_under_its_paths_or_to_its_file_runs_it(self):
        self.story({"paths": [FIXTURE_DIR]})
        self.land("W1", "W2")
        self.loop()
        commit_file(self.repo, "README.md", "more\n", "docs")
        self.loop()

        c = commit_file(self.repo, f"{FIXTURE_DIR}page.txt", "two\n", "page")
        self.loop()
        (w2,) = self.rows("W2", c)
        self.assertIsNone(w2.carriedFrom)
        self.assertEqual(self.counter("W2"), 2)

        d = commit_file(self.repo, "tests/test_w2.py", LANDED + "# again\n",
                        "w2 edited")
        self.loop()
        (w2,) = self.rows("W2", d)
        self.assertIsNone(w2.carriedFrom)
        self.assertEqual(self.counter("W2"), 3)

    def test_an_hours_cadence_carries_until_its_verdict_is_that_old(self):
        self.story({"hours": 6})
        a = self.land("W1", "W2")
        self.loop()
        (real,) = self.rows("W2", a)
        now = int(time.time() * 1000)
        self.conn.execute("UPDATE witnessResults SET at = ? WHERE id = ?",
                          (now - HOUR_MS, real.id))
        self.conn.commit()

        b = commit_file(self.repo, "README.md", "more\n", "docs")
        self.loop()
        (carried,) = self.rows("W2", b)
        self.assertEqual(carried.carriedFrom, a)

        self.conn.execute("UPDATE witnessResults SET at = ? WHERE id = ?",
                          (now - 7 * HOUR_MS, real.id))
        self.conn.commit()
        c = commit_file(self.repo, "README.md", "again\n", "docs")
        self.loop()
        (ran,) = self.rows("W2", c)
        self.assertIsNone(ran.carriedFrom)
        self.assertEqual(self.counter("W2"), 2)

    def test_a_carried_red_owes_no_rerun_and_writes_no_note(self):
        self.story({"paths": [FIXTURE_DIR]})
        self.land("W1", "W2")
        self.loop()
        b = commit_file(self.repo, "tests/test_w2.py", FAILS_AN_ASSERTION,
                        "w2 breaks")
        self.loop()
        self.assertEqual([row.verdict for row in self.rows("W2", b)],
                         ["red", "red"])

        c = commit_file(self.repo, "README.md", "more\n", "docs")
        self.loop()
        self.loop()

        (w2,) = self.rows("W2", c)
        self.assertEqual((w2.verdict, w2.carriedFrom), ("red", b))
        self.assertEqual(rerun_owed(self.conn, self.story_id, c), set())
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM ticketNotes WHERE kind = 'verdict'"
            " AND dedupKey = ?",
            (f"witness:{self.story_id}:W2:{c}",)).fetchone(), (0,))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM ticketNotes WHERE kind = 'verdict'"
            " AND dedupKey = ?",
            (f"witness:{self.story_id}:W2:{b}",)).fetchone(), (1,))

    def test_a_story_closes_on_a_carried_green_and_its_note_says_so(self):
        self.story({"paths": [FIXTURE_DIR]}, sources=(LANDED, LANDED))
        a = self.land("W2")
        self.loop()
        self.assertEqual(store.stories.story(self.conn, self.story_id).state,
                         "approved")

        b = commit_file(self.repo, "tests/test_w1.py", LANDED, "w1 lands")
        self.loop()

        self.assertEqual(self.rows("W2", b)[0].carriedFrom, a)
        found = store.stories.story(self.conn, self.story_id)
        self.assertEqual((found.state, found.closedSha), ("closed", b))
        (note,) = self.conn.execute(
            "SELECT text FROM ticketNotes WHERE kind = 'verdict'"
            " AND dedupKey = ?",
            (f"story-closed:{self.story_id}:{b}",)).fetchone()
        self.assertIn(f"W2 green (carried from {a[:12]})", note)

    def test_a_hand_run_pass_runs_a_witness_that_is_not_due(self):
        self.story({"paths": [FIXTURE_DIR]})
        self.land("W1", "W2")
        self.loop()
        b = commit_file(self.repo, "README.md", "more\n", "docs")

        witness_pass(self.target, self.conn, self.story_id, "operator")

        (w2,) = self.rows("W2", b)
        self.assertEqual((w2.verdict, w2.carriedFrom, w2.verifier),
                         ("green", None, "operator"))
        self.assertEqual(self.counter("W2"), 2)


if __name__ == "__main__":
    unittest.main()
