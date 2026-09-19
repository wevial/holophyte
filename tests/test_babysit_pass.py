"""`holophyte.babysitter`'s pass under `[merge] mode = "pr"`, end to end."""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
# Discovery never imports `fake_agent`; put its `tests/` directory on the path.
# Putting it there explicitly makes `discover -s tests` and `-m unittest
# tests.<name>` resolve the harness the same way.
sys.path.insert(0, str(HERE))
import babysit_fixture as cases  # noqa: E402
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    Commit,
    Idle,
    Reply,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    MergeModeFixture,
)

import holophyte.agents  # noqa: E402 - after the sys.path insert above
import holophyte.operator  # noqa: E402 - after the sys.path insert above
import holophyte.pr  # noqa: E402 - after the sys.path insert above
import holophyte.pr_status  # noqa: E402 - after the sys.path insert above


class MergeModeBabysitPassTests(cases.ConflictRefusalCases, MergeModeFixture):
    """Pass structure, settling, quiet clocks, and refreshing main."""
    def test_closed_pr_is_rejected_mid_pass(self):
        self.configure('[merge]\nmode = "pr"\n')
        state = self.pr_state(checks="PENDING")
        node = state["data"]["repository"]["pullRequest"]
        node.update(state="CLOSED", timelineItems={"nodes": [
            {"actor": {"login": "alice"}}]})
        self.fake_route(states=[state])
        with patch.object(
            holophyte.pr, "SLEEP",
            side_effect=AssertionError("closed PR must not wait"),
        ) as sleep:
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())
        sleep.assert_not_called()
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("rejected", "rejected")])
        self.assertTrue(self.read("SELECT blockedQuestion FROM tickets")
                        [0][0].startswith("rejected:"))
        self.assertIsNone(self.rc)


    def test_closed_pr_at_pass_cap_is_rejected(self):
        self.configure('[merge]\nmode = "pr"\npr_rounds = 1\n')
        closed = self.pr_state()
        closed["data"]["repository"]["pullRequest"].update(
            state="CLOSED", timelineItems={"nodes": [{"actor": {"login": "alice"}}]})
        self.fake_route(states=[self.pr_state([self.DEFECT]), closed])
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  Reply("THREAD 1: ADDRESS -- a real crash"),
                  Commit("fix: default load()"), provider=self.provider())
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("rejected", "rejected")])
        self.assertIsNone(self.rc)


    def test_a_green_pr_quieter_than_pr_quiet_sec_is_not_merged(self):
        self.configure('[merge]\nmode = "pr"\npr_rounds = 1\ncheck_wait_sec = 45\n')
        fresh = (datetime.now(timezone.utc)
                 - timedelta(seconds=10)).isoformat()
        self.fake_route(states=[self.pr_state(updated_at=fresh)])
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append), \
                patch.object(holophyte.babysitter, "monotonic",
                             side_effect=lambda: sum(naps)):
            out = self.main_output(Commit("the scripted work"), APPROVE, Idle(""),
                                   provider=self.provider())

        self.assertTrue(naps)
        self.assertRegex(out, r"green and quiet for \d+s of the 300s"
                              r" required; waiting")
        calls = self.api_calls()
        self.assertGreater(len(calls), 1)
        self.assertEqual({kind for kind, _ in calls}, {"state"})
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertIn("quiet wait exceeded 45s", self.question())


    def test_a_green_pr_quiet_for_pr_quiet_sec_merges(self):
        self.configure('[merge]\nmode = "pr"\n')
        quiet = (datetime.now(timezone.utc)
                 - timedelta(seconds=301)).isoformat()
        self.fake_route(states=[self.pr_state(updated_at=quiet)])
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())

        self.assertEqual(naps, [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])


    def test_pr_quiet_sec_zero_merges_a_green_pr_on_the_first_pass(self):
        self.configure('[merge]\nmode = "pr"\npr_quiet_sec = 0\n')
        self.fake_route(states=[self.pr_state(
            updated_at=datetime.now(timezone.utc).isoformat())])
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())

        self.assertEqual(naps, [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])


    def test_babysit_re_entry_merges_the_approved_sha_without_a_review(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.NIT]), self.pr_state()])
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  Reply("THREAD 1: DECLINE -- a naming preference"),
                  provider=self.provider())
        approved = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.read("SELECT candidateSha, approvedSha FROM"
                                   " runs"), [(approved, approved)])
        for path in self.api_dir.iterdir():
            path.unlink()
        holophyte.operator.babysit_ticket(self.tgt, "KO-131", "nit closed",
                                       out=io.StringIO())

        fake, _ = self.loop(provider=self.provider())

        self.assertEqual(fake.roles, [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"
                                   " WHERE id = 2"),
                         [("merged", self.MERGE_SHA)])


    def test_human_resume_after_launch_loop_waits_without_merging(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(states=[self.pr_state()])
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  provider=self.provider())
        holophyte.operator.babysit_ticket(self.tgt, "KO-131", "look again",
                                         out=io.StringIO())
        import store
        with closing(store.open(self.tgt.store_path)) as conn:
            store.record_intervention(conn, 1, "launch_loop", "resume",
                                      source="supervisor")
        for path in self.api_dir.iterdir():
            path.unlink()

        self.loop(provider=self.provider())

        self.assertFalse([v for kind, v in self.api_calls() if kind == "merge"])
        self.assertEqual(self.read("SELECT phase, outcome FROM runs WHERE id = 2"),
                         [("awaiting_merge_approval", None)])
        self.assertIn("waiting for a human to say merge", self.question())

    def test_threads_past_the_first_page_keep_the_pr_from_reading_quiet(self):
        self.configure('[merge]\nmode = "pr"\n')
        full_page = [self.NIT] * holophyte.pr_status.THREADS_PAGE
        self.fake_route(states=[self.pr_state(resolved=full_page,
                                              next_cursor="c1"),
                                self.pr_state([self.DEFECT])])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: HUMAN -- not mine to answer"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement", "adjudicate"])
        calls = self.api_calls()
        self.assertEqual([kind for kind, _ in calls], ["state", "state"])
        self.assertEqual([v["after"] for _, v in calls], [None, "c1"])
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertIn(self.DEFECT[3], self.question())


if __name__ == "__main__":
    unittest.main()
