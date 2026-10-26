from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit, FakeAgent, no_agent_processes  # noqa: E402
from loop_fixture import LoopFixture, StubProvider, a_task  # noqa: E402

import holophyte.board.projection  # noqa: E402 - after the sys.path insert above
import holophyte.host.startup  # noqa: E402 - after the sys.path insert above
import holophyte.loop.adjudicate  # noqa: E402 - after the sys.path insert above
import holophyte.loop.gate_build  # noqa: E402 - after the sys.path insert above
import holophyte.loop.gates  # noqa: E402 - after the sys.path insert above
import holophyte.loop.implement  # noqa: E402 - after the sys.path insert above
import holophyte.loop.pool  # noqa: E402 - after the sys.path insert above
import holophyte.loop.review_round  # noqa: E402 - after the sys.path insert above
import holophyte.review.findings  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.schema  # noqa: E402 - after the sys.path insert above

BRANCH = "task/ko-131-add-a-thing"


class GateBuildTests(LoopFixture):
    """A pool worker whose factory checkout moved under its run re-executes
    into the merge gate under an unchanged schema and otherwise hands the
    approved candidate to the next build instead of gating it."""

    def worker(self, *script, provider, build, env=()):
        fake = FakeAgent(*script)
        out = io.StringIO()
        with no_agent_processes(), \
                patch.dict(sys.modules,
                           {"holophyte.board.linear_provider": provider}), \
                patch.object(holophyte.loop.implement, "agent", fake), \
                patch.object(holophyte.loop.review_round, "agent", fake), \
                patch.object(holophyte.loop.adjudicate, "agent", fake), \
                patch.object(holophyte.host.startup, "BUILD", build), \
                patch.dict(os.environ, {holophyte.loop.pool.WORKER_SLOT_ENV: "2",
                                        **dict(env)}), \
                patch.object(sys, "stdout", out):
            rc = holophyte.loop.pool.worker(self.project, provider)
        return rc, out.getvalue()

    def test_a_moved_checkout_hands_the_approved_run_off_before_the_gate(self):
        (self.target / "guard.txt").write_text("a guard merged mid-run\n")
        self.git("add", "guard.txt")
        self.git("commit", "-q", "-m", "a guard lands")
        head = self.git("rev-parse", "HEAD").strip()

        rc, _ = self.worker(Commit("the scripted work"), APPROVE,
                            provider=StubProvider(a_task(1)), build=self.base)

        self.assertNotIn(f"Merge {BRANCH}: add a thing", self.subjects())
        self.assertNotIn("reviewing -> merge_gate", self.transitions())
        handoffs = self.read(
            "SELECT summary FROM runEvents WHERE kind = 'gate_handoff'")
        self.assertEqual(len(handoffs), 1, handoffs)
        self.assertIn(self.base[:12], handoffs[0][0])
        self.assertIn(head[:12], handoffs[0][0])
        self.assertEqual(len(self.read(
            "SELECT 1 FROM runEvents WHERE kind = 'approval_carried'")), 1)
        candidate = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(
            self.read("SELECT outcome, resumePhase, candidateSha FROM runs"),
            [("paused", "merge_gate", candidate)])
        self.assertEqual(self.read("SELECT status FROM tickets"), [("ready",)])
        self.assertEqual(rc, holophyte.loop.pool.WORKER_HANDOFF)

    def test_the_next_build_merges_the_handed_off_candidate(self):
        provider = StubProvider(a_task(1))
        (self.target / "guard.txt").write_text("a guard merged mid-run\n")
        self.git("add", "guard.txt")
        self.git("commit", "-q", "-m", "a guard lands")
        self.worker(Commit("the scripted work"), APPROVE,
                    provider=provider, build=self.base)
        candidate = self.git("rev-parse", BRANCH).strip()
        provider.queue.append(a_task(1))

        rc, out = self.worker(provider=provider,
                              build=self.git("rev-parse", "HEAD").strip())

        self.assertIn("skipping to the merge gate", out)
        self.assertEqual(rc, holophyte.loop.pool.WORKER_MERGED)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("paused",), ("merged",)])
        self.assertIn(f"Merge {BRANCH}: add a thing", self.subjects())
        self.git("merge-base", "--is-ancestor", candidate, "main")

    def commit_schema(self, version):
        (self.target / "store").mkdir(exist_ok=True)
        (self.target / "store" / "schema.py").write_text(
            f"SCHEMA_VERSION = {version}\n")
        self.git("add", "store/schema.py")
        self.git("commit", "-q", "-m", "the schema at the checkout's HEAD")
        return self.git("rev-parse", "HEAD").strip()

    def test_a_moved_checkout_under_an_unchanged_schema_reexecutes_in_place(self):
        head = self.commit_schema(store.schema.SCHEMA_VERSION)
        provider = StubProvider(a_task(1))
        calls = []

        def record(*args):
            calls.append(
                (args, os.environ.get("HOLOPHYTE_GATE_RUN"),
                 self.read("SELECT id, endedAt FROM runs"), self.transitions()))

        with patch.object(holophyte.loop.gate_build, "EXEC", record):
            rc, _ = self.worker(Commit("the scripted work"), APPROVE,
                                provider=provider, build=self.base)

        self.assertEqual(self.read(
            "SELECT 1 FROM runEvents WHERE kind = 'gate_handoff'"), [])
        self.assertNotEqual(rc, holophyte.loop.pool.WORKER_HANDOFF)
        self.assertEqual(len(calls), 1, calls)
        _, gate_run, runs, transitions = calls[0]
        (run_id, ended), = runs
        self.assertEqual(gate_run, str(run_id))
        self.assertIsNone(ended)
        self.assertNotIn("reviewing -> merge_gate", transitions)
        candidate = self.git("rev-parse", BRANCH).strip()
        (payload,), = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'gate_reexec'")
        payload = json.loads(payload)
        self.assertEqual((payload["leaving"], payload["arriving"],
                          payload["sha"]), (self.base, head, candidate))

        rc, _ = self.worker(provider=provider, build=head,
                            env={"HOLOPHYTE_GATE_RUN": gate_run})

        self.assertEqual(rc, holophyte.loop.pool.WORKER_MERGED)
        self.assertEqual(self.read("SELECT id, outcome FROM runs"),
                         [(run_id, "merged")])
        self.assertIn(f"Merge {BRANCH}: add a thing", self.subjects())
        self.git("merge-base", "--is-ancestor", candidate, "main")

    def reexecuted(self, provider):
        head = self.commit_schema(store.schema.SCHEMA_VERSION)
        with patch.object(holophyte.loop.gate_build, "EXEC", lambda *a: None):
            self.worker(Commit("the scripted work"), APPROVE,
                        provider=provider, build=self.base)
        (run_id,), = self.read("SELECT id FROM runs")
        return head, {"HOLOPHYTE_GATE_RUN": str(run_id)}

    def test_the_resumed_gate_reports_time_since_the_claim(self):
        provider = StubProvider(a_task(1))
        head, env = self.reexecuted(provider)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE runs SET startedAt = startedAt - 3600000")

        rc, _ = self.worker(provider=provider, build=head, env=env)

        self.assertEqual(rc, holophyte.loop.pool.WORKER_MERGED)
        (text,), = self.read("SELECT text FROM ledger WHERE kind = 'merge'")
        actual = float(text.split("actual: ")[1].split()[0])
        self.assertGreaterEqual(actual, 60)

    def test_a_board_error_after_the_reexec_fails_the_run_for_a_new_claim(self):
        provider = StubProvider(a_task(1))
        head, env = self.reexecuted(provider)
        reads = []

        def down(issue_id):
            reads.append(issue_id)
            raise RuntimeError("the board timed out")

        with patch.object(provider, "fetch_task", down), \
                patch.object(holophyte.loop.gate_build, "BOARD_WAIT_S", 0):
            rc, _ = self.worker(provider=provider, build=head, env=env)

        self.assertEqual(rc, holophyte.loop.pool.WORKER_BOARD_DOWN)
        self.assertGreater(len(reads), 1)
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertNotIn(f"Merge {BRANCH}: add a thing", self.subjects())
        self.assertEqual(provider.labels["iss-131"], [])
        with sqlite3.connect(self.db) as conn:
            store.requeue(conn, 1, "the board answers again")
        provider.queue.append(a_task(1))
        rc, _ = self.worker(Commit("the scripted work again"), APPROVE,
                            provider=provider, build=head)
        self.assertEqual(rc, holophyte.loop.pool.WORKER_MERGED)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("failed",), ("merged",)])

    def test_a_board_error_after_the_reexec_renders_findings_under_the_merge_lock(self):
        self.configure('[report]\nfindings = "repo"\n')
        provider = StubProvider(a_task(1))
        head, env = self.reexecuted(provider)
        lock = holophyte.loop.gates.merge_lock_path(self.project)
        held = []
        real = holophyte.review.findings.refresh_findings

        def render_under_lock(target, conn):
            held.append(lock.exists())
            return real(target, conn)

        def down(issue_id):
            raise RuntimeError("the board timed out")

        with patch.object(provider, "fetch_task", down), \
                patch.object(holophyte.loop.gate_build, "BOARD_WAIT_S", 0), \
                patch.object(holophyte.loop.pool, "refresh_findings",
                             render_under_lock), \
                patch.object(holophyte.board.projection, "refresh_findings",
                             render_under_lock):
            rc, _ = self.worker(provider=provider, build=head, env=env)

        self.assertEqual(rc, holophyte.loop.pool.WORKER_BOARD_DOWN)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(held, [True])
        self.assertIn("KO-131", (self.project.path / "FINDINGS.md").read_text())
        self.assertFalse(lock.exists())

    def test_a_ticket_gone_from_the_board_after_the_reexec_fails_the_run(self):
        provider = StubProvider(a_task(1))
        head, env = self.reexecuted(provider)

        with patch.object(provider, "fetch_task", lambda issue_id: None):
            rc, _ = self.worker(provider=provider, build=head, env=env)

        self.assertEqual(rc, holophyte.loop.pool.WORKER_FAILED)
        self.assertEqual(self.read("SELECT outcome FROM runs WHERE endedAt"
                                   " IS NOT NULL"), [("failed",)])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(provider.labels["iss-131"], [])
        self.assertNotIn(f"Merge {BRANCH}: add a thing", self.subjects())

    def test_a_failed_route_probe_after_the_reexec_hands_the_run_off(self):
        provider = StubProvider(a_task(1))
        head, env = self.reexecuted(provider)
        candidate = self.git("rev-parse", BRANCH).strip()
        down = self.db.parent / "writer.sh"
        down.write_text("#!/bin/sh\nexit 1\n")
        down.chmod(0o755)
        self.configure(f'[agents]\nimplementer = "{down}"\nwriter = "{down}"\n')

        rc, _ = self.worker(provider=provider, build=head, env=env)

        self.assertEqual(rc, holophyte.loop.pool.WORKER_STOP)
        self.assertEqual(
            self.read("SELECT outcome, resumePhase, candidateSha FROM runs"),
            [("paused", "merge_gate", candidate)])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(len(self.read(
            "SELECT 1 FROM runEvents WHERE kind = 'gate_handoff'")), 1)
        self.assertNotIn(f"Merge {BRANCH}: add a thing", self.subjects())

    def test_a_dirty_candidate_after_the_reexec_is_handed_off_not_gated(self):
        provider = StubProvider(a_task(1))
        head, env = self.reexecuted(provider)
        (self.worktrees / BRANCH.split("/", 1)[1] / "stray.txt").write_text(
            "left after the approval\n")

        rc, _ = self.worker(provider=provider, build=head, env=env)

        self.assertEqual(rc, holophyte.loop.pool.WORKER_HANDOFF)
        self.assertNotIn(f"Merge {BRANCH}: add a thing", self.subjects())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("paused",)])
        (summary,), = self.read(
            "SELECT summary FROM runEvents WHERE kind = 'gate_handoff'")
        self.assertIn("uncommitted changes", summary)

    def test_a_moved_checkout_under_a_changed_schema_still_hands_off(self):
        self.commit_schema(store.schema.SCHEMA_VERSION + 1)
        calls = []

        with patch.object(holophyte.loop.gate_build, "EXEC",
                          lambda *a: calls.append(a)):
            rc, _ = self.worker(Commit("the scripted work"), APPROVE,
                                provider=StubProvider(a_task(1)),
                                build=self.base)

        self.assertEqual(calls, [])
        self.assertEqual(rc, holophyte.loop.pool.WORKER_HANDOFF)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("paused",)])
        self.assertEqual(len(self.read(
            "SELECT 1 FROM runEvents WHERE kind = 'gate_handoff'")), 1)
        self.assertEqual(self.read(
            "SELECT 1 FROM runEvents WHERE kind = 'gate_reexec'"), [])

    def test_a_gate_run_that_has_ended_resumes_nothing(self):
        head = self.git("rev-parse", "HEAD").strip()
        provider = StubProvider(a_task(1))
        self.worker(Commit("the scripted work"), APPROVE, provider=provider,
                    build=head)
        (run_id, outcome), = self.read("SELECT id, outcome FROM runs")
        self.assertEqual(outcome, "merged")
        provider.queue.append(a_task(2))

        rc, out = self.worker(provider=provider, build=head,
                              env={"HOLOPHYTE_GATE_RUN": str(run_id)})

        self.assertIn(f"run {run_id} is not waiting at the merge gate;"
                      " nothing to resume", out)
        self.assertEqual(rc, holophyte.loop.pool.WORKER_IDLE)
        self.assertEqual(self.read("SELECT id FROM runs"), [(run_id,)])

    def assert_gated_in_place(self, build):
        rc, _ = self.worker(Commit("the scripted work"), APPROVE,
                            provider=StubProvider(a_task(1)), build=build)

        self.assertEqual(rc, holophyte.loop.pool.WORKER_MERGED)
        self.assertEqual(self.read(
            "SELECT 1 FROM runEvents WHERE kind = 'gate_handoff'"), [])
        self.assertIn(f"Merge {BRANCH}: add a thing", self.subjects())

    def test_an_unmoved_checkout_gates_the_run_in_place(self):
        self.assert_gated_in_place(self.git("rev-parse", "HEAD").strip())

    def test_a_worker_with_no_loaded_build_gates_the_run_in_place(self):
        self.assert_gated_in_place(None)


REEXEC_CHILD = f"""
import os, sys
sys.path.insert(0, {str(ROOT)!r})
from holophyte.loop import gate_build
print("pid", os.getpid(), flush=True)
if os.environ.get("HOLOPHYTE_GATE_RUN"):
    print("gate", os.environ["HOLOPHYTE_GATE_RUN"])
    sys.exit(0)
gate_build.replace_into_gate(7)
"""


class ReplaceIntoGateTests(unittest.TestCase):
    def test_the_worker_reexecutes_in_its_own_pid_with_the_gate_run_set(self):
        env = {k: v for k, v in os.environ.items() if k != "HOLOPHYTE_GATE_RUN"}

        done = subprocess.run([sys.executable, "-c", REEXEC_CHILD], env=env,
                              capture_output=True, text=True, timeout=60)

        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.split("\n")
        pids = [line.split()[1] for line in lines if line.startswith("pid ")]
        self.assertEqual(len(pids), 2, done.stdout)
        self.assertEqual(pids[0], pids[1])
        self.assertIn("gate 7", lines)


if __name__ == "__main__":
    unittest.main()
