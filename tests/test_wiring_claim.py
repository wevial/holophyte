"""Wiring contract: the loop's store bootstrap and its claim-through-the-lease.

The loop opens one WAL-mode store in the target's state directory and routes every
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
import os
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
        self.states = []

    def claim_next(self, skip=()):
        """The first queued task the loop has not already refused.

        `skip` is honored rather than ignored because the real provider hands
        back the *same* head-of-queue ticket on every ask; a stub that popped
        blindly would let a loop that cannot skip look like one that can.
        """
        for i, task in enumerate(self.queue):
            if task["id"] not in skip:
                return self.queue.pop(i)
        return None

    def set_state(self, issue_id, state):
        self.states.append((issue_id, state))


ISSUE_UUID = "9f1c2d34-5678-4abc-9def-0123456789ab"  # Linear's canonical id


def a_task(identifier="HOL-1", title="do the thing", issue_id=ISSUE_UUID):
    """One parsed ticket in the shape `linear_provider.parse_task()` returns.

    Two ids, as the provider gives them: the human `id` and the canonical
    `issue_id` UUID, deliberately unequal so a test cannot pass by storing
    whichever one is at hand.
    """
    return {"id": identifier, "issue_id": issue_id, "title": title,
            "verify": "python3 -m unittest discover -s tests",
            "criteria": ["Given a claim, when it lands, then the mirror exists"],
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
        # Stands in for factory.STORE_PATH: outside the target, never a file in it.
        self.db = Path(tmp.name) / "store.db"
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

    def test_the_store_path_is_in_the_targets_state_directory(self):
        """One store per target, in its directory under `HOLOPHYTE_HOME`.

        Retargets a module of its own, because the paths are derived from the
        target together: patching STORE_PATH afterwards, as the other tests
        do, would test the patch rather than the rule.
        """
        home = Path(tempfile.mkdtemp()) / "home"
        with patch.dict(os.environ, {"HOLOPHYTE_HOME": str(home)}):
            mod = importlib.util.module_from_spec(SPEC)
            SPEC.loader.exec_module(mod)

            mod.retarget("/srv/dev/holo2test", adopt=False)

        self.assertEqual(mod.STORE_PATH.parent.parent, home)
        self.assertEqual(mod.STORE_PATH.name, "store.db")
        self.assertEqual(mod.CONFIG_PATH.parent, mod.STORE_PATH.parent)
        # The worktrees keep their sibling address beside the checkout.
        self.assertEqual(mod.WORKTREES, Path("/srv/dev/holo2test.worktrees"))

    def test_claim_mirrors_the_ticket_and_holds_the_lease_during_the_run(self):
        seen = {}

        def spy(task, conn=None, run_id=None, provider=None):
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
        """The UUID is the mirror's key, so the same issue mirrors once.

        The second offer arrives under a renamed label; a mirror keyed on the
        label would add a second row for it. The store already says `merged`,
        so the loop refuses the re-claim before it mirrors anything — the
        row count is the assertion, not a refreshed label.
        """
        with patch.object(factory, "run_task", return_value=True):
            factory.main(StubProvider(a_task()))
            factory.main(StubProvider(a_task(identifier="HOL-1-renamed")))

        self.assertEqual(self.read("SELECT linearIssueId, status FROM tickets"),
                         [(ISSUE_UUID, "merged")])

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

    def test_a_ticket_the_store_says_is_in_flight_is_skipped_for_the_next(self):
        """The store, not the board, decides what is claimable.

        A failed run leaves its ticket `in_flight` in the store while the
        board offers it again; claiming it anyway produced a run row that
        existed only to be refused. So the loop asks `store.pickable()` first
        and moves on to the next ready ticket, and the skipped one gets no
        run row at all.
        """
        first = a_task(identifier="HOL-1", issue_id=ISSUE_UUID)
        second = a_task(identifier="HOL-2", title="the other thing",
                        issue_id="5e0d1c2b-3a49-4f58-8e67-76543210fedc")
        with patch.object(factory, "run_task", return_value=False):
            factory.main(StubProvider(first))  # fails: mirror stays in_flight
        self.assertEqual(self.read("SELECT linearIdentifier, status FROM tickets"),
                         [("HOL-1", "in_flight")])
        before = self.read("SELECT id, ticketId FROM runs")

        with patch.object(factory, "run_task", return_value=True) as run_task, \
                patch("builtins.print") as printed:
            factory.main(StubProvider(first, second))

        run_task.assert_called_once()
        self.assertEqual(run_task.call_args.args[0]["id"], "HOL-2")
        (skipped_id,), = self.read(
            "SELECT id FROM tickets WHERE linearIdentifier = 'HOL-1'")
        self.assertEqual(
            self.read(f"SELECT id, ticketId FROM runs WHERE ticketId = {skipped_id}"),
            [r for r in before if r[1] == skipped_id])
        self.assertEqual(
            self.read("SELECT t.linearIdentifier FROM runs r"
                      " JOIN tickets t ON t.id = r.ticketId"
                      " WHERE r.id NOT IN (%s)" % ",".join(str(r[0]) for r in before)),
            [("HOL-2",)])
        lines = [c.args[0] for c in printed.call_args_list
                 if c.args and "skipping" in str(c.args[0])]
        self.assertEqual(len(lines), 1)
        self.assertIn("HOL-1", lines[0])
        self.assertIn("in_flight", lines[0])

    def test_a_ready_mirror_whose_live_body_lost_its_contract_is_not_run(self):
        """The gate reads the body the run would work from, not the row a
        previous pass left. The store already says `ready` with both lists;
        the offer arrives with the verify command edited out. Claiming on the
        stale row would open a run and hand `run_task()` an empty contract."""
        stale = a_task()
        conn = factory.open_store(self.db)
        self.addCleanup(conn.close)
        project = store.ensure_project(conn, StubProvider.TEAM, self.target)
        store.mirror_ticket(conn, project, linear_issue_id=ISSUE_UUID,
                            linear_identifier=stale["id"], title=stale["title"],
                            acceptance_criteria=stale["criteria"],
                            verification_commands=[stale["verify"]])
        self.assertEqual(self.read("SELECT status FROM tickets"), [("ready",)])
        live = dict(stale, verify="")

        with patch.object(factory, "run_task", return_value=True) as run_task, \
                patch("builtins.print") as printed:
            factory.main(StubProvider(live))

        run_task.assert_not_called()
        self.assertEqual(self.read("SELECT id FROM runs"), [])
        self.assertEqual(
            self.read("SELECT verificationCommands FROM tickets"), [("[]",)])
        lines = [c.args[0] for c in printed.call_args_list
                 if c.args and "skipping" in str(c.args[0])]
        self.assertEqual(len(lines), 1)
        self.assertIn("HOL-1", lines[0])

    def test_an_under_specced_ticket_the_store_has_never_seen_is_skipped(self):
        """A new ticket is judged on the same gate as a known one: it is
        mirrored, found `needs_spec`, and passed over for the next ready
        ticket without a run row of its own."""
        unspecced = a_task(identifier="HOL-1", issue_id=ISSUE_UUID)
        unspecced["criteria"] = []
        second = a_task(identifier="HOL-2", title="the other thing",
                        issue_id="5e0d1c2b-3a49-4f58-8e67-76543210fedc")

        with patch.object(factory, "run_task", return_value=True) as run_task:
            factory.main(StubProvider(unspecced, second))

        run_task.assert_called_once()
        self.assertEqual(run_task.call_args.args[0]["id"], "HOL-2")
        self.assertEqual(
            self.read("SELECT linearIdentifier, status FROM tickets"
                      " ORDER BY linearIdentifier"),
            [("HOL-1", "needs_spec"), ("HOL-2", "merged")])
        self.assertEqual(
            self.read("SELECT t.linearIdentifier FROM runs r"
                      " JOIN tickets t ON t.id = r.ticketId"),
            [("HOL-2",)])

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
            rc = factory.main(StubProvider(a_task()))

        # Contained, not propagated: the crash is this run's failure, exit 1.
        self.assertEqual(rc, 1)
        self.assertEqual(self.read("SELECT activeRunId FROM projects"), [(None,)])
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("failed", "failed")])


if __name__ == "__main__":
    unittest.main()
