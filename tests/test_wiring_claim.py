"""Wiring contract: the loop's store bootstrap and its claim-through-the-lease.

The loop opens one WAL-mode store beside the target repo and routes every
ticket claim through `store.claim()`, so a second loop on the same project
loses on the lease instead of cutting a branch beside the first one. These
tests read the tables back with their own SQL — the oracle is the stored
state, not the factory's view of it — and drive `main()` with a stub provider
so no Linear call and no git command is involved.

Run: python3 -m unittest discover -s tests -p 'test_wiring*' -v
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
SPEC = importlib.util.spec_from_file_location("holophyte_factory", ROOT / "factory.py")
factory = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(factory)

import store  # noqa: E402 - after the sys.path insert above


class StubProvider:
    """The provider seam `main()` drives: a queue of task dicts, no network."""

    TEAM = "team-under-test"

    def __init__(self, *tasks):
        self.queue = list(tasks)

    def claim_next(self):
        return self.queue.pop(0) if self.queue else None


ISSUE_UUID = "9f1c2d34-5678-4abc-9def-0123456789ab"  # Linear's canonical id


def a_task(identifier="HOL-1", title="do the thing", issue_id=ISSUE_UUID):
    """One parsed ticket in the shape `linear_provider.parse_task()` returns.

    Two ids, as the provider gives them: the human `id` and the canonical
    `issue_id` UUID, deliberately unequal so a test cannot pass by storing
    whichever one is at hand.
    """
    return {"id": identifier, "issue_id": issue_id, "title": title,
            "verify": "python3 -m unittest discover -s tests",
            "contracts": [], "budget_min": 25}


class WiringClaimTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.target = Path(tmp.name) / "repo"
        self.target.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.target, check=True)
        # Close-out commits FINDINGS.md in the target, so the fixture needs an
        # identity of its own: inheriting the developer's global one would pass
        # here and error wherever git is unconfigured.
        for key, value in (("user.email", "factory@example.invalid"),
                           ("user.name", "Factory Test")):
            subprocess.run(["git", "config", key, value],
                           cwd=self.target, check=True)
        # Mirrors factory.STORE_PATH: a sibling of the target, not a file in it.
        self.db = Path(tmp.name) / "repo.holophyte.db"
        self.worktrees = Path(tmp.name) / "repo.worktrees"
        for attr, value in (("TARGET", self.target), ("STORE_PATH", self.db),
                            ("WORKTREES", self.worktrees)):
            p = patch.object(factory, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def read(self, sql):
        """Query the store over a connection the factory never touched."""
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn.execute(sql).fetchall()

    def hold_the_lease(self):
        """Leave the project with an active run, as a second loop would find it."""
        conn = factory.open_store(self.db)
        self.addCleanup(conn.close)
        project = store.ensure_project(conn, StubProvider.TEAM, self.target)
        ticket = store.mirror_ticket(conn, project, "HOL-0", "HOL-0", "in flight")
        return store.claim(conn, project, ticket)

    def test_loop_start_creates_a_wal_store_with_the_schema(self):
        factory.main(StubProvider())  # no ready tickets: bootstrap and stop

        self.assertTrue(self.db.exists())
        self.assertEqual(self.read("PRAGMA journal_mode")[0][0].lower(), "wal")
        tables = {r[0] for r in
                  self.read("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertLessEqual({"projects", "tickets", "runs"}, tables)

    def test_the_store_leaves_the_target_checkout_clean(self):
        """The store is the loop's file, not the target repo's.

        Asserted against a real `git status` rather than against a .gitignore
        entry: the factory repo's ignore rules say nothing about the target
        checkout, so the only thing that keeps the database and its two WAL
        sidecars out of a task's `git add -A` is living outside the repo.
        """
        factory.main(StubProvider())  # bootstrap the store, no ready tickets

        self.assertTrue(self.db.exists())
        self.assertFalse(self.db.is_relative_to(self.target))
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=self.target, capture_output=True, text=True, check=True)
        self.assertEqual(status.stdout, "")

    def test_the_store_path_is_a_sibling_of_the_target(self):
        """One store per target, named after it, beside it -- like WORKTREES.

        Re-imports the module under a target argv, because the path is derived
        once at import: patching STORE_PATH afterwards, as the other tests do,
        would test the patch rather than the rule.
        """
        with patch.object(sys, "argv", ["factory.py", "/srv/dev/holo2test"]):
            mod = importlib.util.module_from_spec(SPEC)
            SPEC.loader.exec_module(mod)

        self.assertEqual(mod.STORE_PATH, Path("/srv/dev/holo2test.holophyte.db"))
        self.assertEqual(mod.STORE_PATH.parent, mod.WORKTREES.parent)

    def test_claim_mirrors_the_ticket_and_holds_the_lease_during_the_run(self):
        seen = {}

        def spy(task, conn=None, run_id=None):
            # Runs while the lease is held, and before run_task's first git
            # command — so this is the state the branch would be cut under.
            seen["projects"] = self.read("SELECT id, activeRunId FROM projects")
            seen["tickets"] = self.read(
                "SELECT id, projectId, linearIssueId, linearIdentifier, title,"
                " verificationCommands, timeBoxMs, activeRunId FROM tickets")
            seen["runs"] = self.read(
                "SELECT id, ticketId, projectId, attempt, phase FROM runs")
            return True

        with patch.object(factory, "run_task", spy):
            factory.main(StubProvider(a_task()))

        (project_id, project_lease), = seen["projects"]
        (ticket_id, ticket_project, issue_id, identifier, title,
         commands, time_box, ticket_lease), = seen["tickets"]
        (run_id, run_ticket, run_project, attempt, phase), = seen["runs"]
        # The mirror is keyed on the canonical UUID, not on the human label:
        # the label moves, and a webhook only ever carries the UUID.
        self.assertEqual((issue_id, identifier, title),
                         (ISSUE_UUID, "HOL-1", "do the thing"))
        self.assertEqual(json.loads(commands), [a_task()["verify"]])
        self.assertEqual(time_box, 25 * 60 * 1000)
        self.assertEqual((run_ticket, run_project, attempt, phase),
                         (ticket_id, project_id, 1, "claimed"))
        self.assertEqual(ticket_project, project_id)
        self.assertEqual((project_lease, ticket_lease), (run_id, run_id))

    def test_a_re_claimed_ticket_reuses_its_mirror_rather_than_adding_one(self):
        """The UUID is the mirror's key, so the same issue mirrors once."""
        with patch.object(factory, "run_task", return_value=True):
            factory.main(StubProvider(a_task()))
            factory.main(StubProvider(a_task(identifier="HOL-1-renamed")))

        self.assertEqual(
            self.read("SELECT linearIssueId, linearIdentifier FROM tickets"),
            [(ISSUE_UUID, "HOL-1-renamed")])

    def test_a_provider_without_a_uuid_still_mirrors_under_its_identifier(self):
        """A UUID-less provider keeps working, keyed on the id it does have."""
        with patch.object(factory, "run_task", return_value=True):
            factory.main(StubProvider(a_task(issue_id=None)))

        self.assertEqual(
            self.read("SELECT linearIssueId, linearIdentifier FROM tickets"),
            [("HOL-1", "HOL-1")])

    def test_a_second_claim_is_refused_before_any_branch_is_cut(self):
        held = self.hold_the_lease()

        with patch.object(factory, "run_task") as run_task:
            factory.main(StubProvider(a_task()))

        run_task.assert_not_called()
        self.assertFalse(self.worktrees.exists())
        self.assertEqual(self.read("SELECT id FROM runs"), [(held,)])

    def test_a_merged_run_gives_the_lease_back(self):
        with patch.object(factory, "run_task", return_value=True):
            factory.main(StubProvider(a_task()))

        self.assertEqual(self.read("SELECT activeRunId FROM projects"), [(None,)])
        (run_id, phase, outcome, ended), = self.read(
            "SELECT id, phase, outcome, endedAt FROM runs")
        self.assertEqual((phase, outcome), ("done", "merged"))
        self.assertIsNotNone(ended)
        self.assertEqual(self.read("SELECT activeRunId, lastRunId FROM tickets"),
                         [(None, run_id)])

    def test_a_failed_run_gives_the_lease_back(self):
        with patch.object(factory, "run_task", return_value=False):
            factory.main(StubProvider(a_task()))

        self.assertEqual(self.read("SELECT activeRunId FROM projects"), [(None,)])
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("failed", "failed")])

    def test_a_crashed_run_does_not_leave_the_lease_held(self):
        boom = RuntimeError("merge blew up")

        with patch.object(factory, "run_task", side_effect=boom):
            with self.assertRaises(RuntimeError):
                factory.main(StubProvider(a_task()))

        self.assertEqual(self.read("SELECT activeRunId FROM projects"), [(None,)])
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("failed", "failed")])


if __name__ == "__main__":
    unittest.main()
