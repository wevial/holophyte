"""A story child's feature follow-up, settled at its run's merge, becomes a
proposal of the story or a related standalone draft by the adjudicator's
call; a duplicate, or a ticket outside an open story, never reaches the seat.

Run: python3 -m unittest discover -s tests -p 'test_story_proposals.py' -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402
from story_fixture import child_body, write_story  # noqa: E402

import store  # noqa: E402
import store.board  # noqa: E402
import store.follow_ups  # noqa: E402
import store.read  # noqa: E402
import store.story_proposals  # noqa: E402
import store.tickets  # noqa: E402
import ticket_template  # noqa: E402
from holophyte.agents import probes  # noqa: E402
from holophyte.agents.agent_routes import reset  # noqa: E402
from holophyte.board.projection import release_run  # noqa: E402
from holophyte.loop.follow_ups import capture, fingerprint, settle  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from provider import board_for  # noqa: E402
from store.read import claimable  # noqa: E402
from store.stories import (  # noqa: E402
    approve_story,
    close_story,
    file_story,
    story,
    story_frontier,
)

NATIVE = '[board]\nkind = "native"\nkey = "NAT"\n'
FEATURE = "FOLLOW_UP(feature): Export the refund column @ export.py:42"
TEXT = "Export the refund column"
IN_STORY = "I read the story.\nSCOPE: in_story: completes W2's last case"
STANDALONE = "SCOPE: standalone: a board concern, not the story's"
OUTAGE = "ERROR: You've hit your usage limit"
WITNESSES = [
    {"key": key, "criterion": f"outcome {key}", "file": f"tests/test_{key}.py",
     "command": f"python3 -m unittest tests.test_{key}", "source": "pass\n"}
    for key in ("W1", "W2")]

PROBE_GOAL = probes.REVIEW_PROBE_GOAL
ROUTE = """#!{python}
import json, subprocess, sys
goal = sys.argv[-1]
with open({calls!r}, "a") as f:
    f.write(json.dumps({{"route": {name!r}, "goal": goal}}) + "\\n")
if goal == {probe!r}:
    print("ready " + subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True).strip())
    sys.exit(0)
for needle, (reply, code) in json.load(open({replies!r})).items():
    if needle in goal:
        print(reply)
        sys.exit(code)
print("no reply scripted")
"""


def events(conn, run_id, kind):
    return [(summary, json.loads(payload)) for summary, payload in conn.execute(
        "SELECT summary, payload FROM runEvents WHERE runId = ? AND kind = ?"
        " ORDER BY seq", (run_id, kind))]


class StoryProposalTests(LoopFixture):
    """A native project over a real store; the seat is a real command."""

    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        environ = patch.dict(os.environ, env, clear=True)
        environ.start()
        self.addCleanup(environ.stop)
        self.calls = self.target.parent / "calls.jsonl"
        self.replies = self.target.parent / "replies.json"
        self.seat("codex-adjudicator")
        self.board = board_for(self.project)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, self.board.team, self.project.path)

    def route(self, name):
        path = self.target.parent / name
        path.write_text(ROUTE.format(
            python=sys.executable, calls=str(self.calls), name=name,
            probe=PROBE_GOAL, replies=str(self.replies)))
        path.chmod(0o755)
        return str(path)

    def seat(self, primary, fallback=None):
        config = f'{NATIVE}[agents]\nadjudicator = "{self.route(primary)}"\n'
        if fallback:
            config += f'adjudicator_fallback = "{self.route(fallback)}"\n'
        self.configure(config)
        self.addCleanup(reset, self.project)

    def reply(self, **by_text):
        self.replies.write_text(json.dumps(
            {needle: [reply, code] for needle, (reply, code) in by_text.items()}))

    def turns(self, probes=False):
        if not self.calls.exists():
            return []
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return [call for call in calls
                if probes or call["goal"] != PROBE_GOAL]

    def file(self, body, column="backlog"):
        key = store.board.file_ticket(self.conn, self.project_id, "NAT", body,
                                      column=column)
        return store.read.ticket_by_identifier(self.conn, key).id

    def approved_story(self, second="backlog"):
        """NAT-1 the story, NAT-2 its child to merge, NAT-3 its second child."""
        with tempfile.TemporaryDirectory() as tmp:
            parent = self.file((write_story(tmp) / "story.md").read_text())
        self.merged_child = self.file(child_body("one", "advances W1"))
        self.backlog_child = self.file(child_body("two", "completes W1, W2"),
                                       column=second)
        file_story(self.conn, parent,
                   WITNESSES, [(self.merged_child, "advances", ("W1",)),
                               (self.backlog_child, "completes", ("W1", "W2"))])
        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (parent,)).fetchone()
        approve_story(self.conn, parent, revision, "operator", "go")
        self.parent = parent
        return parent

    def claimed(self, ticket_id, *messages):
        run_id = store.claim(self.conn, self.project_id, ticket_id)
        base = self.git("rev-parse", "HEAD").strip()
        for message in messages:
            self.git("commit", "-q", "--allow-empty", "-m", message)
        capture(self.conn, run_id, self.target, base)
        for phase in ("working", "verifying", "reviewing", "merge_gate",
                      "merging"):
            store.set_phase(self.conn, run_id, phase)
        return run_id

    def release(self, run_id, merge_sha=None):
        release_run(self.conn, run_id, True, merge_sha=merge_sha
                    or self.git("rev-parse", "HEAD").strip())

    def merge(self, run_id, merge_sha=None):
        self.release(run_id, merge_sha)
        settle(self.project, self.conn, run_id)

    def merged(self, ticket_id, *messages):
        run_id = self.claimed(ticket_id, *messages)
        self.merge(run_id)
        return run_id

    def proposals(self):
        return self.read("SELECT id, storyId, followUpId, raisedBy, state,"
                         " childTicketId FROM storyProposals ORDER BY id")

    def ticket_count(self):
        return self.read("SELECT COUNT(*) FROM tickets")[0][0]

    def drafts(self):
        return self.read("SELECT linearIdentifier, body, parentTicketId"
                         " FROM tickets WHERE title LIKE 'Draft follow-up: %'"
                         " ORDER BY id")


class ScopeCallTests(StoryProposalTests):
    def test_the_goal_holds_the_story_its_children_and_the_follow_up(self):
        self.approved_story()
        self.reply(**{TEXT: (IN_STORY, 0)})

        self.merged(self.merged_child, f"fix\n\n{FEATURE}")

        [turn] = self.turns()
        goal = turn["goal"]
        for fact in ("Orders export as CSV", "Orders can be exported as CSV.",
                     "An operator downloads every order as one CSV file.",
                     '- NAT-2 "Orders export step one" (merged)',
                     '- NAT-3 "Orders export step two" (ready)',
                     "Raised by: NAT-2", f"Follow-up: {TEXT}",
                     "Found at: export.py:42"):
            self.assertIn(fact, goal)

    def test_a_merge_made_on_the_remote_is_fetched_for_the_scope_turn(self):
        self.approved_story()
        self.reply(**{TEXT: (IN_STORY, 0)})
        origin = self.target.parent / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(origin))
        self.git("remote", "add", "origin", str(origin))
        self.git("push", "-q", "origin", "main")
        remote = self.target.parent / "remote-checkout"
        self.git("clone", "-q", str(origin), str(remote))
        self.git("-c", "user.name=Remote", "-c", "user.email=r@example.invalid",
                 "commit", "-q", "--allow-empty", "-m", "merge on the remote",
                 cwd=remote)
        self.git("push", "-q", "origin", "main", cwd=remote)
        merge_sha = self.git("rev-parse", "HEAD", cwd=remote).strip()
        run_id = self.claimed(self.merged_child, f"fix\n\n{FEATURE}")

        self.merge(run_id, merge_sha)

        self.assertEqual(len(self.turns()), 1)
        [(_, payload)] = events(self.conn, run_id, "follow_up_scope")
        self.assertEqual((payload["verdict"], payload["default"]),
                         ("in_story", False))
        self.assertEqual(len(self.proposals()), 1)

    def test_an_in_story_reply_writes_a_proposal_and_files_nothing(self):
        parent = self.approved_story()
        self.reply(**{TEXT: (IN_STORY, 0)})
        tickets = self.ticket_count()

        run_id = self.merged(self.merged_child, f"fix\n\n{FEATURE}")

        [(proposal, story_id, follow_up, raised_by, state, child)] = (
            self.proposals())
        self.assertEqual((story_id, raised_by, state, child),
                         (parent, self.merged_child, "proposed", None))
        self.assertEqual(self.ticket_count(), tickets)
        self.assertEqual(self.read(
            f"SELECT filedAs, settledAt IS NOT NULL FROM followUps"
            f" WHERE id = {follow_up}"), [(None, 1)])
        [(summary, payload)] = events(self.conn, run_id, "follow_up_scope")
        self.assertIn("in_story", summary)
        self.assertIn("completes W2's last case", summary)
        self.assertEqual((payload["id"], payload["default"]),
                         (follow_up, False))
        [(_, proposed)] = events(self.conn, run_id, "follow_up_proposed")
        self.assertEqual(proposed["key"], f"p{proposal}")
        (body,) = self.read(f"SELECT body FROM storyProposals"
                            f" WHERE id = {proposal}")[0]
        parsed = ticket_template.parse(body)
        self.assertIn("Proposed child of story NAT-1, raised by NAT-2;",
                      parsed.summary)
        self.assertIn("Depends on: NAT-2", body)

    def test_a_proposal_leaves_the_story_its_frontier_and_claims_unchanged(self):
        parent = self.approved_story(second="ready")
        self.reply(**{TEXT: (IN_STORY, 0)})
        run_id = self.claimed(self.merged_child, f"fix\n\n{FEATURE}")
        self.release(run_id)

        def facts():
            return (story(self.conn, parent),
                    story_frontier(self.conn, parent, 2),
                    claimable(self.conn, self.project_id))
        before = facts()
        settle(self.project, self.conn, run_id)

        self.assertEqual(len(self.proposals()), 1)
        self.assertEqual(facts(), before)
        self.assertEqual(before[1], ["NAT-3"])

    def test_a_standalone_reply_files_a_related_draft_with_no_parent(self):
        self.approved_story()
        self.reply(**{TEXT: (STANDALONE, 0)})

        run_id = self.merged(self.merged_child, f"fix\n\n{FEATURE}")

        [(key, body, parent)] = self.drafts()
        self.assertIsNone(parent)
        self.assertIn("Related: story NAT-1, raised by its child NAT-2;",
                      ticket_template.parse(body).summary)
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM storyChildren WHERE ticketId ="
            f" (SELECT id FROM tickets WHERE linearIdentifier = '{key}')"),
            [(0,)])
        self.assertEqual(self.proposals(), [])
        [(summary, payload)] = events(self.conn, run_id, "follow_up_scope")
        self.assertIn("standalone", summary)
        self.assertFalse(payload["default"])

    def test_a_failed_or_unreadable_reply_defaults_to_standalone(self):
        self.approved_story()
        cases = {"Exit one": ("SCOPE: in_story: fine", 1),
                 "No scope": ("I think it belongs in the story.", 0),
                 "Unsure": ("SCOPE: unsure: cannot tell", 0)}
        self.reply(**cases)
        lines = "".join(f"FOLLOW_UP(feature): {text} case\n" for text in cases)

        run_id = self.merged(self.merged_child, f"fix\n\n{lines}")

        self.assertEqual(len(self.turns()), 3)
        self.assertEqual(self.proposals(), [])
        self.assertEqual(len(self.drafts()), 3)
        scopes = events(self.conn, run_id, "follow_up_scope")
        self.assertEqual([payload["default"] for _, payload in scopes],
                         [True, True, True])
        for (summary, _), cause in zip(scopes, (
                "the adjudicator exited 1", "the reply has no SCOPE: line",
                "SCOPE: unsure: cannot tell")):
            self.assertIn("standalone by default", summary)
            self.assertIn(cause, summary)
        for _, body, _ in self.drafts():
            self.assertIn("Related: story NAT-1", body)


class ScopeFallbackTests(StoryProposalTests):
    def test_an_outage_moves_the_turn_to_the_configured_fallback(self):
        self.seat("codex-adjudicator", "devin-fallback")
        self.approved_story()
        self.reply(**{"devin-never": ("", 0)})
        fallback_reply = self.target.parent / "devin-fallback"
        fallback_reply.write_text(fallback_reply.read_text().replace(
            "for needle", f"if {TEXT!r} in goal:\n    print({IN_STORY!r})\n"
            "    sys.exit(0)\nfor needle"))
        primary = self.target.parent / "codex-adjudicator"
        primary.write_text(primary.read_text().replace(
            "for needle", f"print({OUTAGE!r})\nsys.exit(1)\nfor needle"))

        run_id = self.merged(self.merged_child, f"fix\n\n{FEATURE}")

        self.assertEqual([(turn["route"], turn["goal"] == PROBE_GOAL)
                          for turn in self.turns(probes=True)],
                         [("codex-adjudicator", False),
                          ("devin-fallback", True),
                          ("devin-fallback", False)])
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM runEvents WHERE kind = 'route_fallback'"
            f" AND runId = {run_id}"), [(1,)])
        [(_, payload)] = events(self.conn, run_id, "follow_up_scope")
        self.assertEqual((payload["verdict"], payload["default"]),
                         ("in_story", False))
        self.assertIn("devin-fallback", payload["route"])
        self.assertEqual(len(self.proposals()), 1)

    def test_an_outage_with_no_fallback_defaults_and_runs_no_other_route(self):
        self.approved_story()
        self.reply(**{TEXT: (OUTAGE, 1)})

        run_id = self.merged(self.merged_child, f"fix\n\n{FEATURE}")

        self.assertEqual([turn["route"] for turn in self.turns(probes=True)],
                         ["codex-adjudicator"])
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM runEvents WHERE kind = 'route_fallback'"),
            [(0,)])
        [(summary, payload)] = events(self.conn, run_id, "follow_up_scope")
        self.assertTrue(payload["default"])
        self.assertIn("exited 1", summary)
        self.assertEqual(self.proposals(), [])
        self.assertEqual(len(self.drafts()), 1)

    def test_an_outage_exiting_zero_with_a_scope_line_still_defaults(self):
        self.approved_story()
        self.reply(**{TEXT: (f"{OUTAGE}\n{IN_STORY}", 0)})

        run_id = self.merged(self.merged_child, f"fix\n\n{FEATURE}")

        self.assertEqual([turn["route"] for turn in self.turns(probes=True)],
                         ["codex-adjudicator"])
        [(summary, payload)] = events(self.conn, run_id, "follow_up_scope")
        self.assertEqual((payload["verdict"], payload["default"]),
                         ("standalone", True))
        self.assertIn(OUTAGE, summary)
        self.assertEqual(self.proposals(), [])
        [(_, body, parent)] = self.drafts()
        self.assertIn("Related: story NAT-1", body)
        self.assertIsNone(parent)


class NoScopeCallTests(StoryProposalTests):
    def test_a_duplicate_of_a_child_a_rejected_proposal_or_a_draft_is_settled(self):
        self.approved_story()
        loose = self.file(VALID_BODY, column="ready")
        other_run = self.merged(
            loose, "fix\n\nFOLLOW_UP(feature): Drafted elsewhere @ a.py:1")
        [(draft, _, _)] = self.drafts()
        rejected = store.follow_ups.record_follow_up(
            self.conn, other_run, "c0ffee", "feature", True, "Refused before",
            fingerprint("Refused before", "b.py"), path="b.py", line=2)
        proposal = store.story_proposals.record_proposal(
            self.conn, self.parent, rejected, self.merged_child, "title", "body")
        self.conn.execute("UPDATE storyProposals SET state = 'rejected'")
        self.conn.commit()
        tickets = self.ticket_count()

        run_id = self.merged(self.merged_child, "fix\n\n"
                             "FOLLOW_UP(feature): orders export  STEP one.\n"
                             "FOLLOW_UP(feature): refused before @ b.py:9\n"
                             "FOLLOW_UP(feature): Drafted elsewhere @ a.py:5")

        self.assertEqual(self.turns(probes=True), [])
        self.assertEqual(self.ticket_count(), tickets)
        self.assertEqual(len(self.proposals()), 1)
        self.assertEqual([payload["key"] for _, payload in events(
            self.conn, run_id, "follow_up_duplicate")],
            ["NAT-2", f"p{proposal}", draft])
        self.assertEqual(events(self.conn, run_id, "follow_up_scope"), [])

    def test_a_ticket_in_no_story_or_a_closed_story_files_a_plain_draft(self):
        self.approved_story()
        run_id = self.claimed(self.merged_child, f"fix\n\n{FEATURE}")
        close_story(self.conn, self.parent, "0a0b0c", "done")
        self.merge(run_id)
        loose = self.file(VALID_BODY, column="ready")
        self.merged(loose, "fix\n\nFOLLOW_UP(feature): Outside any story")

        self.assertEqual(self.turns(probes=True), [])
        drafts = self.drafts()
        self.assertEqual(len(drafts), 2)
        for _, body, parent in drafts:
            self.assertNotIn("Related:", body)
            self.assertIsNone(parent)
        self.assertEqual(self.proposals(), [])


if __name__ == "__main__":
    unittest.main()
