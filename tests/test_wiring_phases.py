"""Wiring contract: run phase tracking and heartbeats (state-model §4).

The loop records where a run is as it moves: each stage boundary writes the
run's phase, stamps its heartbeat and appends one narrative `phase_change`
event, so in-flight state is durable rather than living in the loop's process
memory. These tests drive `main()` over a real throwaway repo with only the
agent turns faked — the loop's own git, worktree, verify and merge steps run
for real — and read the phases back with their own SQL, so the oracle is the
stored stream and not the factory's view of it.

The expected sequences below are transcribed from §4's diagram by hand.

Run: python3 -m unittest discover -s tests -p 'test_wiring*' -v
"""
from __future__ import annotations

import importlib.util
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
    """The provider seam `main()` drives, plus the module `run_task` imports."""

    TEAM = "team-under-test"

    def __init__(self, *tasks):
        self.queue = list(tasks)
        self.states = []
        self.comments = []

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

    def comment(self, task_id, body):
        self.comments.append((task_id, body))


class RunPhaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.target = root / "repo"
        self.target.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "Factory Test")
        (self.target / "README.md").write_text("base\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")

        self.db = root / "repo.holophyte.db"
        # The `Target` the loop is handed, with the store and the worktrees
        # placed by hand: outside the target, never a file in it.
        self.tgt = factory.Target(
            path=self.target, holo_dir=root, store_path=self.db,
            config_path=root / "config.toml",
            worktrees=root / "repo.worktrees")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.target),
                              check=True, capture_output=True, text=True).stdout

    def loop(self, *replies, provider=None):
        """Run the loop over one task, answering each review turn in order."""
        replies = list(replies)
        turns = []

        def fake_agent(target, role, goal, cwd, *, base_sha=None,
                       candidate_sha=None, timeout=None):
            turns.append(role)
            if role != "implement":
                return replies.pop(0)
            n = sum(1 for turn in turns if turn == "implement")
            (Path(cwd) / f"change{n}.txt").write_text(f"work {n}\n")
            self.git("add", "-A", cwd=cwd)
            self.git("commit", "-q", "-m", f"work {n}", cwd=cwd)
            return f"committed work {n}"

        provider = provider or StubProvider(
            {"id": "KO-129", "issue_id": "iss-129", "title": "add a thing",
             "verify": "echo ok", "budget_min": 5, "contracts": [],
             "criteria": ["Given the thing, when it runs, then it works"]})
        with patch.dict(sys.modules, {"linear_provider": provider}):
            with patch.object(factory, "agent", fake_agent):
                factory.main(self.tgt, provider)
        return provider

    def read(self, sql):
        """Query the store over a connection the factory never touched."""
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn.execute(sql).fetchall()

    def events(self):
        return self.read("SELECT seq, level, kind, summary, at FROM runEvents"
                         " ORDER BY seq")

    def transitions(self):
        """The edges the run's narrative stream says it walked.

        Each `phase_change` summary opens with `"<from> -> <to>"` and may
        carry a note after a colon; the edge is what §4 draws, so the note is
        dropped here. Every walked edge must be one `RUN_PHASE_TRANSITIONS`
        draws: that table is README's run diagram, and this is where the
        loop's real stream is held against it.
        """
        edges = [summary.split(":")[0] for _, _, _, summary, _ in self.events()]
        for edge in edges:
            src, dst = edge.split(" -> ")
            self.assertIn(dst, store.RUN_PHASE_TRANSITIONS[src],
                          f"walked edge {edge!r} is not in RUN_PHASE_TRANSITIONS")
        return edges

    def notes(self):
        return [summary.split(": ", 1)[1] for _, _, _, summary, _ in self.events()
                if ": " in summary]

    def run_row(self):
        (row,) = self.read("SELECT phase, outcome, outcomeReason, resumePhase,"
                           " startedAt, lastHeartbeat, endedAt FROM runs")
        return row

    # --- the merged run --------------------------------------------------

    def test_a_merged_run_walks_the_documented_phases_and_ends_done(self):
        """§4 from the claim to the merge: claimed → working → verifying →
        reviewing → merge_gate → merging, ending `done` with outcome merged.
        `squashing` is absent because this loop merges --no-ff and rewrites no
        history, so it never does the thing that phase names."""
        provider = self.loop(
            "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
            "VERDICT: APPROVE")

        # The merge's Linear side is the projection and nothing else: the
        # claim posted In Progress, the merge posted Done.
        self.assertEqual(provider.states,
                         [("iss-129", "In Progress"), ("iss-129", "Done")])
        self.assertEqual(self.transitions(),
                         ["claimed -> working",
                          "working -> verifying",
                          "verifying -> reviewing",
                          "reviewing -> merge_gate",
                          "merge_gate -> merging",
                          "merging -> done"])
        phase, outcome, _, resume_phase, _, _, ended = self.run_row()
        self.assertEqual((phase, outcome), ("done", "merged"))
        self.assertIsNone(resume_phase)  # nothing to resume a merged run into
        self.assertIsNotNone(ended)

    def test_phase_changes_are_narrative_events_numbered_per_run(self):
        """§2's dual-level log: transitions are `narrative`/`phase_change`
        rows, so the dashboard's default view is the run's story, and `seq` is
        monotonic from 1 without a caller counting."""
        self.loop("CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
            "VERDICT: APPROVE")

        events = self.events()
        self.assertEqual([seq for seq, *_ in events],
                         list(range(1, len(events) + 1)))
        self.assertEqual({(level, kind) for _, level, kind, _, _ in events},
                         {("narrative", "phase_change")})

    # --- the failed run --------------------------------------------------

    def test_a_run_that_fails_review_records_where_it_stopped(self):
        """Two rounds of findings and a failing adjudication: the run row
        keeps outcome failed with a reason, and the phase it stopped in
        survives as the phase §5 would resume it into."""
        self.loop("VERDICT: REQUEST_CHANGES", "VERDICT: REQUEST_CHANGES",
                  "Broken.\nVERDICT: FAIL")

        self.assertEqual(self.transitions(),
                         ["claimed -> working",
                          "working -> verifying",
                          "verifying -> reviewing",
                          "reviewing -> addressing",
                          "addressing -> verifying",
                          "verifying -> reviewing",
                          "reviewing -> addressing",
                          "addressing -> verifying",
                          "verifying -> reviewing",
                          "reviewing -> failed"])
        # The two review rounds are distinguishable in the stream: a phase
        # name alone cannot say which round a repeated edge belongs to.
        self.assertIn("round 2: addressing findings", self.notes())
        phase, outcome, reason, resume_phase, *_ = self.run_row()
        self.assertEqual((phase, outcome, resume_phase),
                         ("failed", "failed", "reviewing"))
        self.assertIn("terminal adjudication", reason)
        self.assertIn("preserved at", reason)

    def test_a_crashed_run_keeps_the_phase_it_died_in(self):
        """The point of durable phases: state that outlives the process. The
        loop dies mid-review with no chance to record anything, and the run
        still says the work stopped under review."""
        def boom(target, role, goal, cwd, *, base_sha=None,
                 candidate_sha=None, timeout=None):
            if role == "review":
                raise RuntimeError("reviewer host went away")
            (Path(cwd) / "change.txt").write_text("work\n")
            self.git("add", "-A", cwd=cwd)
            self.git("commit", "-q", "-m", "work", cwd=cwd)
            return "committed"

        provider = StubProvider(
            {"id": "KO-129", "issue_id": "iss-129", "title": "add a thing",
             "verify": "echo ok", "budget_min": 5, "contracts": [],
             "criteria": ["Given the thing, when it runs, then it works"]})
        with patch.dict(sys.modules, {"linear_provider": provider}):
            with patch.object(factory, "agent", boom):
                rc = factory.main(self.tgt, provider)

        # Contained, not propagated — and the run row still says the work
        # stopped under review, with the error text as the reason.
        self.assertEqual(rc, 1)
        self.assertEqual(self.transitions()[-2:],
                         ["verifying -> reviewing", "reviewing -> failed"])
        phase, outcome, reason, resume_phase, *_ = self.run_row()
        self.assertEqual((phase, outcome, resume_phase),
                         ("failed", "failed", "reviewing"))
        self.assertIn("reviewer host went away", reason)

    # --- heartbeats ------------------------------------------------------

    def test_the_heartbeat_tracks_the_latest_phase_change(self):
        """A phase and a heartbeat from different moments would show the
        supervisor a run that has been stale for a whole stage, so the run's
        heartbeat is the timestamp of its most recent transition."""
        self.loop("CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
            "VERDICT: APPROVE")

        _, _, _, _, started, heartbeat, _ = self.run_row()
        self.assertEqual(heartbeat, max(at for *_, at in self.events()))
        self.assertGreaterEqual(heartbeat, started)


class PhaseWriteTests(unittest.TestCase):
    """The store primitive the wiring above stands on: one transaction for the
    phase, the heartbeat and the event."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(Path(tmp.name) / "store.sqlite3")
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.ensure_project(self.conn, "team", Path(tmp.name) / "repo")
        ticket = store.mirror_ticket(self.conn, project, "iss-1", "HOL-1", "a ticket")
        self.run_id = store.claim(self.conn, project, ticket, now=1000)

    def state(self):
        (phase, heartbeat), = self.conn.execute(
            "SELECT phase, lastHeartbeat FROM runs WHERE id = ?", (self.run_id,))
        events = self.conn.execute(
            "SELECT summary, at FROM runEvents WHERE runId = ? ORDER BY seq",
            (self.run_id,)).fetchall()
        return phase, heartbeat, events

    def test_a_phase_change_moves_phase_heartbeat_and_event_together(self):
        previous = store.set_phase(self.conn, self.run_id, "working", now=2500)

        self.assertEqual(previous, "claimed")
        self.assertEqual(self.state(),
                         ("working", 2500, [("claimed -> working", 2500)]))

    def test_a_refused_phase_writes_none_of_the_three(self):
        """One transaction, so a rejected write leaves no heartbeat claiming
        liveness and no event describing a transition that never happened."""
        store.set_phase(self.conn, self.run_id, "working", now=2500)

        with self.assertRaises(ValueError):
            store.set_phase(self.conn, self.run_id, "shipping", now=9000)

        self.assertEqual(self.state(),
                         ("working", 2500, [("claimed -> working", 2500)]))


class ReleaseTests(unittest.TestCase):
    """`release()` writes the run's ending exactly once: it moves phase as
    well as outcome, so a stray second call must not rewrite either."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(Path(tmp.name) / "store.sqlite3")
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project = store.ensure_project(self.conn, "team", Path(tmp.name) / "repo")
        ticket = store.mirror_ticket(
            self.conn, self.project, "iss-1", "HOL-1", "a ticket"
        )
        self.run_id = store.claim(self.conn, self.project, ticket, now=1000)

    def run_row(self):
        return self.conn.execute(
            "SELECT phase, outcome, outcomeReason, resumePhase, endedAt"
            " FROM runs WHERE id = ?", (self.run_id,)).fetchone()

    def transitions(self):
        return [summary for (summary,) in self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? ORDER BY seq",
            (self.run_id,))]

    def test_a_merged_run_stays_merged_when_released_again(self):
        store.set_phase(self.conn, self.run_id, "working", now=2000)
        store.release(self.conn, self.run_id, "merged", now=3000)

        store.release(self.conn, self.run_id, "failed", reason="stray", now=4000)

        self.assertEqual(self.run_row(), ("done", "merged", None, None, 3000))
        # And the ignored call left nothing in the stream either: an event
        # saying `done -> failed` would describe a transition that never was.
        self.assertEqual(
            self.transitions()[-1:], ["working -> done: run ended, outcome merged"]
        )

    def test_re_releasing_a_failed_run_keeps_its_resume_phase(self):
        store.set_phase(self.conn, self.run_id, "working", now=2000)
        store.set_phase(self.conn, self.run_id, "reviewing", now=2500)
        store.release(self.conn, self.run_id, "failed", reason="reviewer died",
                      now=3000)

        store.release(self.conn, self.run_id, "failed", reason="stray", now=4000)

        self.assertEqual(
            self.run_row(),
            ("failed", "failed", "reviewer died", "reviewing", 3000))

    def test_a_resumed_run_can_be_released_again(self):
        """The guard is about ended runs, not about run ids seen before:
        `resume()` puts a failed run back to work, and the release that ends
        that second stretch has to land."""
        store.set_phase(self.conn, self.run_id, "working", now=2000)
        store.release(self.conn, self.run_id, "failed", now=3000)
        self.assertEqual(store.resume(self.conn, self.run_id, now=3500), "working")

        store.release(self.conn, self.run_id, "merged", now=4000)

        self.assertEqual(self.run_row(), ("done", "merged", None, None, 4000))


if __name__ == "__main__":
    unittest.main()
