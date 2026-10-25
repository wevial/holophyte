from __future__ import annotations

import io
import os
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

import holophyte.host.startup  # noqa: E402 - after the sys.path insert above
import holophyte.loop.adjudicate  # noqa: E402 - after the sys.path insert above
import holophyte.loop.gate_build  # noqa: E402 - after the sys.path insert above
import holophyte.loop.implement  # noqa: E402 - after the sys.path insert above
import holophyte.loop.pool  # noqa: E402 - after the sys.path insert above
import holophyte.loop.review_round  # noqa: E402 - after the sys.path insert above
import store.schema  # noqa: E402 - after the sys.path insert above

BRANCH = "task/ko-131-add-a-thing"


class GateBuildTests(LoopFixture):
    """A pool worker whose factory checkout moved under its run hands the
    approved candidate to the next build instead of gating it."""

    def worker(self, *script, provider, build):
        fake = FakeAgent(*script)
        out = io.StringIO()
        with no_agent_processes(), \
                patch.dict(sys.modules,
                           {"holophyte.board.linear_provider": provider}), \
                patch.object(holophyte.loop.implement, "agent", fake), \
                patch.object(holophyte.loop.review_round, "agent", fake), \
                patch.object(holophyte.loop.adjudicate, "agent", fake), \
                patch.object(holophyte.host.startup, "BUILD", build), \
                patch.dict(os.environ, {holophyte.loop.pool.WORKER_SLOT_ENV: "2"}), \
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

    def test_a_moved_checkout_under_an_unchanged_schema_reexecutes_in_place(self):
        (self.target / "store").mkdir(exist_ok=True)
        (self.target / "store" / "schema.py").write_text(
            f"SCHEMA_VERSION = {store.schema.SCHEMA_VERSION}\n")
        self.git("add", "store/schema.py")
        self.git("commit", "-q", "-m", "the schema stays put")
        calls = []

        def record(*args):
            calls.append(
                (args, os.environ.get("HOLOPHYTE_GATE_RUN"),
                 self.read("SELECT id, endedAt FROM runs")))

        with patch.object(holophyte.loop.gate_build, "EXEC", record,
                          create=True):
            rc, _ = self.worker(Commit("the scripted work"), APPROVE,
                                provider=StubProvider(a_task(1)),
                                build=self.base)

        self.assertEqual(self.read(
            "SELECT 1 FROM runEvents WHERE kind = 'gate_handoff'"), [])
        self.assertNotEqual(rc, holophyte.loop.pool.WORKER_HANDOFF)
        self.assertEqual(len(calls), 1, calls)
        (run_id, ended), = calls[0][2]
        self.assertEqual(calls[0][1], str(run_id))
        self.assertIsNone(ended)

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


if __name__ == "__main__":
    unittest.main()
