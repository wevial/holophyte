"""Wiring contract: FINDINGS.md as a windowed deterministic rendering.

The file stopped being the source of truth. `runs` and `reviewRounds` hold the
complete history, and `render_findings()` draws a bounded recent window over
them that the loop regenerates at every close-out. These tests assert the three
properties that makes the file safe to overwrite: the window is capped and says
how much it is not showing, one store state renders to exactly one byte string,
and the pre-store history above the marker is never touched.

Run: python3 -m unittest discover -s tests -p 'test_wiring*' -v
"""
from __future__ import annotations

import importlib.util
import json
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


class RenderedWindowTests(unittest.TestCase):
    """The rendering itself, over runs written straight into a store."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.conn = store.open(str(self.root / "holophyte.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project = store.ensure_project(self.conn, "team-1", self.root / "repo")

    def complete_run(self, n):
        """One merged run of its own ticket, stamped a minute apart per `n`."""
        ticket = store.mirror_ticket(
            self.conn, self.project,
            linear_issue_id=f"issue-{n}", linear_identifier=f"KO-{n}",
            title=f"ticket {n}", time_box_ms=25 * 60 * 1000)
        at = 1_700_000_000_000 + n * 60_000
        run_id = store.claim(self.conn, self.project, ticket, now=at)
        store.release(self.conn, run_id, "merged", now=at + 30_000)
        return run_id

    def test_the_window_keeps_the_newest_entries_and_counts_the_rest(self):
        """30 completed runs render as the newest 25 plus one archive line."""
        for n in range(1, 31):
            self.complete_run(n)

        rendered = factory.render_findings(self.conn)

        headings = [line for line in rendered.splitlines()
                    if line.startswith("## ")]
        self.assertEqual(len(headings), factory.FINDINGS_WINDOW)
        self.assertIn(
            "[5 earlier entries in holophyte.db — query runs/reviewRounds]",
            rendered)
        # The five it dropped are the five oldest, and they are the only ones
        # missing: a window that kept the wrong end would still count right.
        self.assertEqual([heading.split(" — ")[1] for heading in headings],
                         [f"KO-{n}" for n in range(6, 31)])
        # Rendered from the rows, not from a clock the loop happened to read.
        self.assertIn("actual: 0.5 min · estimate: 25 min · rounds: 0",
                      rendered)

    def test_two_renders_of_one_store_are_byte_identical(self):
        for n in range(1, 4):
            self.complete_run(n)
        run_id = self.complete_run(4)
        store.record_review_round(
            self.conn, run_id, 1, "changes_requested", "codex-sol-medium",
            findings=[{"path": "store.py", "line": 7, "severity": "p0",
                       "message": "the migration is missing"}],
            started_at=1_700_000_100_000, ended_at=1_700_000_160_000)

        first = factory.render_findings(self.conn)
        second = factory.render_findings(self.conn)

        self.assertEqual(first, second)
        # And identical read back over a connection of its own, so the answer
        # cannot depend on anything this one accumulated.
        other = store.open(str(self.root / "holophyte.db"))
        self.addCleanup(other.close)
        self.assertEqual(first, factory.render_findings(other))

    def test_regeneration_preserves_the_frozen_preamble(self):
        preamble = ("## 2026-08-22T06:21:20Z — KO-105\n"
                    "MERGED to main. Verify: passed. Rounds used: 1.\n")
        path = self.root / "FINDINGS.md"
        path.write_text(preamble)
        self.complete_run(1)

        factory.write_findings(self.conn, path)
        first = path.read_text()
        self.complete_run(2)
        factory.write_findings(self.conn, path)
        second = path.read_text()

        # Untouched, and still the top of the file after a second pass that
        # had a marker to find rather than a bare ledger.
        self.assertTrue(first.startswith(preamble), first[:200])
        self.assertTrue(second.startswith(preamble), second[:200])
        self.assertEqual(second.count(factory.FINDINGS_MARKER), 1)
        self.assertEqual(second.count("MERGED to main. Verify: passed."), 1)
        self.assertIn("KO-2", second)

    def test_history_that_mentions_the_marker_is_still_frozen_whole(self):
        """Only a marker on its own line is the boundary.

        Prose above it is pre-store history the reviewer wrote, and this file
        is one of the things reviewers write about: history cut short at a
        sentence that happens to name the marker is history destroyed, since
        no row holds it.
        """
        preamble = ("## 2026-08-22T06:21:20Z — KO-105\n"
                    "The `<!-- store-rendered below -->` marker splits the file.\n"
                    "\n## 2026-08-23T09:00:00Z — KO-106\nMERGED to main.\n")
        path = self.root / "FINDINGS.md"
        path.write_text(preamble)
        self.complete_run(1)

        factory.write_findings(self.conn, path)
        factory.write_findings(self.conn, path)

        rendered = path.read_text()
        self.assertTrue(rendered.startswith(preamble), rendered[:300])
        self.assertEqual(len([line for line in rendered.splitlines()
                              if line.strip() == factory.FINDINGS_MARKER]), 1)


class CloseOutRegenerationTests(unittest.TestCase):
    """The loop's half: one full task over a real repo, with only the agent
    turns faked, asserted over the file the run left behind."""

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
        for name, value in (("TARGET", self.target), ("STORE_PATH", self.db),
                            ("WORKTREES", root / "repo.worktrees")):
            patcher = patch.object(factory, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.target),
                              check=True, capture_output=True, text=True).stdout

    def loop(self, *replies):
        replies = list(replies)
        turns = []

        def fake_agent(role, goal, cwd, *, base_sha=None, candidate_sha=None,
                       timeout=None):
            turns.append(role)
            if role != "implement":
                return replies.pop(0)
            n = sum(1 for turn in turns if turn == "implement")
            (Path(cwd) / f"change{n}.txt").write_text(f"work {n}\n")
            self.git("add", "-A", cwd=cwd)
            self.git("commit", "-q", "-m", f"work {n}", cwd=cwd)
            return f"committed work {n}"

        provider = StubProvider(
            {"id": "KO-131", "issue_id": "iss-131", "title": "add a thing",
             "verify": "echo ok", "budget_min": 5, "contracts": [],
             "criteria": ["Given the thing, when it runs, then it works"]})
        with patch.dict(sys.modules, {"linear_provider": provider}):
            with patch.object(factory, "agent", fake_agent):
                factory.main(provider)

    def test_a_close_out_renders_and_commits_the_runs_entries(self):
        self.loop("- store.py:7: the migration is missing\n"
                  "VERDICT: REQUEST_CHANGES",
                  "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
            "VERDICT: APPROVE")

        findings = (self.target / "FINDINGS.md").read_text()
        self.assertIn(factory.FINDINGS_MARKER, findings)
        # The round the reviewer filed, the approval that ended the loop, and
        # the merged run's own close-out entry -- which only exists because the
        # window is rendered after the run is released.
        self.assertIn("Round 1: changes_requested · reviewer "
                      "codex-sol-medium · verify passed", findings)
        self.assertIn("- store.py:7 [p2] store.py:7: the migration is missing",
                      findings)
        self.assertIn("Round 2: pass · reviewer codex-sol-medium", findings)
        self.assertRegex(findings,
                         r"MERGED to main\.\nactual: \d+\.\d min · "
                         r"estimate: 5 min · rounds: 2\n")
        self.assertEqual(len([line for line in findings.splitlines()
                              if line.startswith("## ")]), 3)
        # Committed, not left dirty, and by the task's own close-out commit.
        self.assertEqual(
            self.git("status", "--porcelain", "FINDINGS.md").strip(), "")
        self.assertEqual(self.git("log", "-1", "--format=%s", "main").strip(),
                         "Complete task KO-131: add a thing")


if __name__ == "__main__":
    unittest.main()


class MalformedRoundRowTests(unittest.TestCase):
    """The renderer never raises on a row the writer would not have written.

    The rows are the history and the file is a rendering of them; a close-out
    that crashes on one bad row leaves FINDINGS.md stale for every good row
    after it. So a `reviewRounds` row carrying junk -- hand-edited, written by
    an earlier release, or corrupted -- renders as a visible placeholder and
    the window around it renders as usual.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.conn = store.open(str(self.root / "holophyte.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.ensure_project(self.conn, "team-1", self.root / "repo")
        ticket = store.mirror_ticket(
            self.conn, project, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="ticket 1",
            time_box_ms=25 * 60 * 1000)
        self.run_id = store.claim(self.conn, project, ticket,
                                  now=1_700_000_000_000)

    def raw_round(self, number, findings, results="[]"):
        """A row written past the writer: the columns as the schema holds them."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO reviewRounds (runId, round, verificationResults,"
                " verdict, findings, findingsFingerprint, reviewerModel,"
                " startedAt, endedAt) VALUES (?, ?, ?, 'changes_requested',"
                " ?, ?, 'codex-sol-medium', ?, ?)",
                (self.run_id, number, results, findings,
                 store.EMPTY_FINGERPRINT, 1_700_000_000_000 + number * 60_000,
                 1_700_000_030_000 + number * 60_000))

    def test_malformed_timestamps_render_as_placeholders(self):
        self.raw_round(1, "[]")
        self.conn.execute(
            "UPDATE reviewRounds SET startedAt = 'not-a-timestamp',"
            " endedAt = NULL WHERE round = 1")
        self.raw_round(2, "[]")
        self.conn.execute(
            "UPDATE reviewRounds SET endedAt = 1e300 WHERE round = 2")
        self.raw_round(3, "[]")
        self.conn.execute(
            "UPDATE runs SET startedAt = 'later', endedAt = ?,"
            " outcome = 'merged', phase = 'done' WHERE id = ?",
            (1_700_000_500_000, self.run_id))
        self.conn.commit()

        rendered = factory.render_findings(self.conn)

        for number in range(1, 4):
            self.assertIn(f"Round {number}:", rendered)
        self.assertIn("## (unreadable timestamp) — ", rendered)
        self.assertIn("## 2023-11-14T22:16:50Z — KO-1\nRound 3:", rendered)
        # Unplaceable in time, so unreadable rows sort ahead of dated ones.
        self.assertLess(rendered.index("Round 1:"), rendered.index("Round 3:"))
        self.assertIn("MERGED to main", rendered)
        self.assertIn("actual: n/a", rendered)
        self.assertEqual(rendered, factory.render_findings(self.conn))

    def test_malformed_round_rows_render_as_placeholders(self):
        self.raw_round(1, "not json at all")
        self.raw_round(2, '{"path": "store.py"}')  # JSON, but not a list
        self.raw_round(3, '[1, {"message": "no path or severity"},'
                          ' {"path": "a.py", "severity": "p0",'
                          ' "line": {"x": 1}, "message": ["not", "text"]}]')
        self.raw_round(4, "[]", results="[4, 5]")
        self.raw_round(5, "[]", results="{broken")
        store.record_review_round(
            self.conn, self.run_id, 6, "pass", "codex-sol-medium",
            started_at=1_700_000_360_000, ended_at=1_700_000_390_000)

        rendered = factory.render_findings(self.conn)

        for number in range(1, 7):
            self.assertIn(f"Round {number}:", rendered)
        self.assertIn("Round 6: pass", rendered)
        self.assertEqual(rendered.count("Findings: unparseable"), 2)
        self.assertIn("not json at all", rendered)  # the raw column value
        self.assertIn("- a.py:{'x': 1} [p0] ['not', 'text']", rendered)
        self.assertEqual(rendered.count("- (malformed finding)"), 2)
        self.assertEqual(rendered.count("verify unreadable"), 2)
        # Still a function of the rows alone.
        self.assertEqual(rendered, factory.render_findings(self.conn))

    def test_pathologically_nested_findings_do_not_overflow_the_render(self):
        """Depth, not syntax, is the other way a JSON column refuses to decode.

        `json.loads` answers a document nested past the interpreter's C stack
        with `RecursionError`, which is not a `ValueError`; a close-out that let
        it out would be exactly the crash this renderer exists to prevent.
        """
        nested = "[" * 400_000 + "]" * 400_000
        with self.assertRaises(RecursionError):
            json.loads(nested)  # the column really is undecodable, not just odd
        self.raw_round(1, nested)
        self.raw_round(2, "[]", results=nested)

        rendered = factory.render_findings(self.conn)

        self.assertIn("Round 1: changes_requested", rendered)
        self.assertIn("Findings: unparseable", rendered)
        self.assertIn("Round 2: changes_requested · reviewer codex-sol-medium"
                      " · verify unreadable", rendered)
