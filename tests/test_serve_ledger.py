"""`/runs/N/ledger`, `/runs/N/files` and the ledger window and wait: the
run and ledger read routes (`holophyte.serve_runs`) over the socket.

Run: python3 -m unittest discover -s tests -p 'test_serve_ledger*' -v
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from time import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serve_fixture import MERGE_SHA, MIN, ServeTestCase  # noqa: E402 - after the insert

import holophyte.files  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from tests.phase_fixture import finish_run


class RunLedgerTests(ServeTestCase):
    """`/runs/N/ledger`: the run's narrative as the store holds it, oldest
    first, with its kinds; 404 for a run the store has not seen."""

    def seed_ledger(self):
        """One merged run with three ledger entries, written newest-kind
        last but with the middle one stamped oldest, so the order the
        daemon answers is the store's `at` order and not insertion order."""
        self.now = int(time() * 1000)
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            ticket = store.tickets.mirror_ticket(
                conn, project, linear_issue_id="issue-11",
                linear_identifier="KO-11", title="ticket 11",
                acceptance_criteria=["Given ticket 11, then it is worked"],
                verification_commands=["echo ok"], time_box_ms=25 * MIN)
            store.tickets.transition(conn, ticket, "in_flight")
            started = self.now - 30 * MIN
            self.run = store.claim(conn, project, ticket, now=started)
            self.seeded = [
                ("round", "Round 1: changes_requested", "loop",
                 started + 5 * MIN),
                ("note", "operator looked in", "operator", started + 2 * MIN),
                ("merge", "MERGED to main", "loop", started + 20 * MIN),
            ]
            for kind, text, source, at in self.seeded:
                store.record_ledger(conn, self.run, kind, text, source=source,
                                    now=at)
            finish_run(conn, self.run, "merged", now=started + 20 * MIN,
                          merge_sha=MERGE_SHA)
        finally:
            conn.close()

    def test_entries_come_back_oldest_first_with_their_kinds(self):
        self.seed_ledger()
        self.start()

        code, headers, body = self.request("GET", f"/runs/{self.run}/ledger")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body["run_id"], self.run)
        self.assertEqual(body["ticket"], "KO-11")
        self.assertEqual(
            body["entries"],
            [{"at": at, "kind": kind, "text": text, "source": source}
             for kind, text, source, at in sorted(self.seeded,
                                                  key=lambda e: e[3])])
        self.assertEqual([e["kind"] for e in body["entries"]],
                         ["note", "round", "merge"])

    def test_no_such_run_is_404_and_a_non_integer_is_400(self):
        self.seed_ledger()
        self.start()

        code, _, body = self.request("GET", f"/runs/{self.run + 1}/ledger")
        self.assertEqual(code, 404)
        self.assertEqual(body["run"], self.run + 1)
        self.assertIn("error", body)

        code, _, body = self.request("GET", "/runs/eleven/ledger")
        self.assertEqual(code, 400)
        self.assertIn("error", body)


class LedgerWindowTests(ServeTestCase):
    """`/ledger?since=MS`: the ledger across runs newest first, narrowed by
    `kind` or `ticket`; 400 naming a bad parameter."""

    def seed_ledger(self):
        """Two merged runs on two tickets with one entry each of `merge`,
        `intervention` and `round` at T1 < T2 < T3, the newest written
        first so the answer's order is the store's `at` order."""
        self.now = int(time() * 1000)
        started = self.now - 60 * MIN
        self.t1, self.t2, self.t3 = (started + 5 * MIN, started + 10 * MIN,
                                     started + 20 * MIN)
        with patch("store.schema.time.time", return_value=started / 1000):
            conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            self.runs = {}
            self.seeded = []
            # One lease per project: each run is claimed, written and
            # released before the next; the entries themselves are stamped
            # newest first within a run, so the answer's order is `at`.
            for n, entries in ((12, [(self.t3, "round", "Round 1: pass",
                                      "loop")]),
                               (11, [(self.t2, "intervention",
                                      "answered: ship it", "operator"),
                                     (self.t1, "merge", "MERGED to main",
                                      "loop")])):
                ticket = store.tickets.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{n}",
                    linear_identifier=f"KO-{n}", title=f"ticket {n}",
                    acceptance_criteria=[f"Given ticket {n}, then worked"],
                    verification_commands=["echo ok"], time_box_ms=25 * MIN)
                store.tickets.transition(conn, ticket, "in_flight")
                run = store.claim(conn, project, ticket, now=started)
                for at, kind, text, source in entries:
                    store.record_ledger(conn, run, kind, text, source=source,
                                        now=at)
                    self.seeded.append((at, run, f"KO-{n}", kind, text,
                                        source))
                finish_run(conn, run, "merged", now=started + 30 * MIN,
                              merge_sha=MERGE_SHA)
        finally:
            conn.close()

    @staticmethod
    def entry(at, run, ticket, kind, text, source):
        body = {"at": at, "run": run, "ticket": ticket, "kind": kind,
                "source": source, "text": text}
        if kind == "intervention":
            # KO-308: nothing waited before this seeded step, so the two
            # fields ride along as null.
            body.update(cleared=None, waited_ms=None)
        return body

    def test_window_is_newest_first_and_leaves_out_older_rows(self):
        self.seed_ledger()
        self.start()

        code, headers, body = self.request("GET", f"/ledger?since={self.t2}")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body["since"], self.t2)
        self.assertEqual(body["limit"], 200)
        self.assertEqual(body["entries"],
                         [self.entry(*self.seeded[0]),
                          self.entry(*self.seeded[1])])
        self.assertNotIn(self.t1, [e["at"] for e in body["entries"]])

    def test_kind_and_ticket_narrow_the_window(self):
        self.seed_ledger()
        self.start()

        code, _, body = self.request(
            "GET", f"/ledger?since={self.t1}&kind=intervention")
        self.assertEqual(code, 200)
        self.assertEqual(body["entries"], [self.entry(*self.seeded[1])])

        code, _, body = self.request(
            "GET", f"/ledger?since={self.t1}&ticket=KO-11")
        self.assertEqual(code, 200)
        self.assertEqual(body["entries"], [self.entry(*self.seeded[1]),
                                           self.entry(*self.seeded[2])])

        code, _, body = self.request(
            "GET", f"/ledger?since={self.t1}&limit=1")
        self.assertEqual(code, 200)
        self.assertEqual(body["limit"], 1)
        self.assertEqual(body["entries"], [self.entry(*self.seeded[0])])

    def test_bad_parameters_are_400_naming_them(self):
        self.seed_ledger()
        self.start()

        for query, name in (("", "since"), ("since=x", "since"),
                            (f"since={self.t1}&limit=0", "limit"),
                            (f"since={self.t1}&kind=nope", "kind")):
            with self.subTest(query=query):
                code, _, body = self.request("GET", f"/ledger?{query}")
                self.assertEqual(code, 400)
                self.assertIn(name, body["error"])


class LedgerWaitTests(ServeTestCase):
    """KO-308: an `intervention` entry of either ledger read says what the
    operator's step cleared and how long that waited, from the entry's own
    run: the newest `redirect` strictly before it or the run's `endedAt`,
    whichever is newer; other kinds carry neither field."""

    def open_store(self):
        conn = store.open(str(self.db))
        store.init(conn)
        self.project_id = store.tickets.ensure_project(conn, "team-1", self.target)
        return conn

    def claim(self, conn, n, now):
        ticket = store.tickets.mirror_ticket(
            conn, self.project_id, linear_issue_id=f"issue-{n}",
            linear_identifier=f"KO-{n}", title=f"ticket {n}",
            acceptance_criteria=[f"Given ticket {n}, then worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MIN)
        store.tickets.transition(conn, ticket, "in_flight")
        return ticket, store.claim(conn, self.project_id, ticket, now=now)

    @staticmethod
    def ask(conn, run, now):
        store.record_intervention(
            conn, run, "redirect", "asked the operator", source="supervisor",
            trigger="off_criteria", question="Which flag name?", now=now)

    @staticmethod
    def interventions(entries):
        return [e for e in entries if e["kind"] == "intervention"
                and e.get("action") != "migrate"]

    def test_a_resume_clears_the_question_and_the_redirect_pairs_with_nothing(self):
        now = int(time() * 1000)
        started = now - 60 * MIN
        t1, t2 = started + 5 * MIN, started + 18 * MIN
        conn = self.open_store()
        try:
            _, run = self.claim(conn, 21, started)
            self.ask(conn, run, t1)
            store.record_intervention(conn, run, "resume", "answered: keep it",
                                      now=t2)
        finally:
            conn.close()
        self.start()

        code, _, body = self.request("GET", "/ledger?since=0")

        self.assertEqual(code, 200)
        resume, redirect = self.interventions(body["entries"])
        self.assertEqual(resume["at"], t2)
        self.assertEqual(resume["cleared"], "question")
        self.assertEqual(resume["waited_ms"], t2 - t1)
        self.assertEqual(redirect["at"], t1)
        self.assertIsNone(redirect["cleared"])
        self.assertIsNone(redirect["waited_ms"])

    def test_a_requeue_after_a_failure_clears_the_failure(self):
        now = int(time() * 1000)
        started = now - 60 * MIN
        t1, t2 = started + 9 * MIN, started + 40 * MIN
        conn = self.open_store()
        try:
            ticket, run = self.claim(conn, 22, started)
            store.release(conn, run, "failed", reason="verify red", now=t1)
            store.requeue(conn, ticket, "operator requeued", now=t2)
        finally:
            conn.close()
        self.start()

        code, _, body = self.request("GET", f"/runs/{run}/ledger")

        self.assertEqual(code, 200)
        (requeue,) = self.interventions(body["entries"])
        self.assertEqual(requeue["at"], t2)
        self.assertEqual(requeue["cleared"], "failed")
        self.assertEqual(requeue["waited_ms"], t2 - t1)

    def test_the_newer_mark_wins_and_other_kinds_carry_neither_field(self):
        now = int(time() * 1000)
        started = now - 60 * MIN
        t1, t2, t3 = started + 5 * MIN, started + 12 * MIN, started + 30 * MIN
        conn = self.open_store()
        try:
            # KO-23 asked at T1, failed at T2: the failure is the newer mark.
            ticket_a, run_a = self.claim(conn, 23, started)
            store.record_ledger(conn, run_a, "round", "Round 1: pass",
                                now=started + MIN)
            self.ask(conn, run_a, t1)
            store.release(conn, run_a, "failed", reason="verify red", now=t2)
            store.requeue(conn, ticket_a, "operator requeued", now=t3)
            # KO-24 failed at T1, asked at T2: the question is the newer mark.
            ticket_b, run_b = self.claim(conn, 24, started + MIN)
            store.release(conn, run_b, "failed", reason="verify red", now=t1)
            self.ask(conn, run_b, t2)
            store.requeue(conn, ticket_b, "operator requeued", now=t3)
            # KO-25 asked and failed in the same millisecond T2: the
            # redirect is not strictly newer, so the failure wins.
            ticket_c, run_c = self.claim(conn, 25, started + 2 * MIN)
            self.ask(conn, run_c, t2)
            store.release(conn, run_c, "failed", reason="verify red", now=t2)
            store.requeue(conn, ticket_c, "operator requeued", now=t3)
        finally:
            conn.close()
        self.start()

        for run, cleared, mark in ((run_a, "failed", t2),
                                   (run_b, "question", t2),
                                   (run_c, "failed", t2)):
            with self.subTest(run=run, cleared=cleared):
                code, _, body = self.request("GET", f"/runs/{run}/ledger")
                self.assertEqual(code, 200)
                requeue = [e for e in self.interventions(body["entries"])
                           if e["at"] == t3]
                self.assertEqual(len(requeue), 1)
                self.assertEqual(requeue[0]["cleared"], cleared)
                self.assertEqual(requeue[0]["waited_ms"], t3 - mark)

        code, _, body = self.request("GET", f"/runs/{run_a}/ledger")
        others = [e for e in body["entries"] if e["kind"] != "intervention"]
        self.assertIn("round", [e["kind"] for e in others])
        for entry in others:
            self.assertNotIn("cleared", entry)
            self.assertNotIn("waited_ms", entry)


class RunFilesTests(ServeTestCase):
    """`/runs/N/files`: the paths a run touched, from git in the target's
    checkout, for a live branch and for a landed merge."""

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.target, check=True,
                              capture_output=True, text=True).stdout.strip()

    def build_repo(self):
        """`main` with two files; `task/ko-7` adding `new.txt` (2 lines)
        and rewriting one of `kept.txt`'s three lines; a binary blob too,
        on the branch, so its zero counts are witnessed."""
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "factory")
        (self.target / "kept.txt").write_text("one\ntwo\nthree\n")
        (self.target / "other.txt").write_text("untouched\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")
        self.branch = "task/ko-7"
        self.git("checkout", "-q", "-b", self.branch)
        (self.target / "new.txt").write_text("alpha\nbeta\n")
        (self.target / "kept.txt").write_text("one\nTWO\nthree\n")
        (self.target / "blob.bin").write_bytes(bytes(range(256)))
        self.git("add", ".")
        self.git("commit", "-q", "-m", "work")
        self.git("checkout", "-q", "main")
        # main moves on after the branch was cut: a live run's range must
        # start at the merge base, not at main's head.
        (self.target / "other.txt").write_text("main moved\n")
        self.git("commit", "-q", "-am", "main moves on")

    EXPECTED = [
        {"path": "blob.bin", "status": "A", "added": 0, "deleted": 0},
        {"path": "kept.txt", "status": "M", "added": 1, "deleted": 1},
        {"path": "new.txt", "status": "A", "added": 2, "deleted": 0},
    ]

    def set_branch(self, branch):
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute("UPDATE runs SET branch = ? WHERE id = ?",
                         (branch, self.run))
            conn.commit()
        finally:
            conn.close()

    def merge(self):
        """Land the branch on main with `--no-ff` and release the run as
        merged with the merge commit's sha, as the loop does."""
        self.git("merge", "--no-ff", "-q", "-m", "land ko-7", self.branch)
        sha = self.git("rev-parse", "HEAD")
        conn = store.open(str(self.db))
        try:
            finish_run(conn, self.run, "merged", now=self.now,
                          merge_sha=sha)
        finally:
            conn.close()
        return sha

    def setUp(self):
        super().setUp()
        self.build_repo()
        self.seed()
        self.set_branch(self.branch)
        self.start()

    def test_a_live_run_lists_its_branch_against_the_merge_base(self):
        code, headers, body = self.request("GET", f"/runs/{self.run}/files")
        self.assertEqual(code, 200, body)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body["files"], self.EXPECTED)
        self.assertEqual(body["run"], self.run)
        self.assertEqual(body["base"], self.git("merge-base", "main", self.branch))
        self.assertEqual(body["head"], self.git("rev-parse", self.branch))
        self.assertEqual(body["total_added"], 3)
        self.assertEqual(body["total_deleted"], 1)
        self.assertFalse(body["truncated"])

    def test_a_merged_run_lists_the_merge_against_its_first_parent(self):
        sha = self.merge()
        # The branch is gone, as a close-out may leave it: the merge sha
        # alone must carry the answer.
        self.git("branch", "-D", self.branch)
        code, _headers, body = self.request("GET", f"/runs/{self.run}/files")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["head"], sha)
        self.assertEqual(body["base"], self.git("rev-parse", f"{sha}^1"))
        self.assertEqual(body["files"], self.EXPECTED)
        self.assertEqual((body["total_added"], body["total_deleted"]), (3, 1))

    def test_a_deleted_branch_is_409_naming_it_and_no_run_is_404(self):
        self.git("branch", "-D", self.branch)
        code, headers, body = self.request("GET", f"/runs/{self.run}/files")
        self.assertEqual(code, 409, body)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn(self.branch, body["error"])
        self.set_branch(None)
        code, _headers, body = self.request("GET", f"/runs/{self.run}/files")
        self.assertEqual(code, 409, body)
        self.assertIn("error", body)
        code, _headers, body = self.request("GET", "/runs/999/files")
        self.assertEqual(code, 404)
        self.assertEqual(body, {"error": "no such run", "run": 999})
        code, _headers, body = self.request("GET", "/runs/abc/files")
        self.assertEqual(code, 400)

    def test_a_deleted_branch_is_409_even_when_a_same_name_tag_survives(self):
        # A bare name would fall through to the tag and answer 200; the
        # run's branch is gone and the endpoint must say so.
        self.git("tag", self.branch, self.branch)
        self.git("branch", "-D", self.branch)
        code, _headers, body = self.request("GET", f"/runs/{self.run}/files")
        self.assertEqual(code, 409, body)
        self.assertIn(self.branch, body["error"])

    def test_the_list_is_capped_and_truncated_past_it(self):
        with patch.object(holophyte.files, "MAX_FILES", 2):
            code, _headers, body = self.request("GET", f"/runs/{self.run}/files")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["files"], self.EXPECTED[:2])
        self.assertTrue(body["truncated"])
        # The totals still count the whole diff.
        self.assertEqual((body["total_added"], body["total_deleted"]), (3, 1))


if __name__ == "__main__":
    unittest.main()
