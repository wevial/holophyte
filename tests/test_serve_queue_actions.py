"""`POST /actions/queue-jump` and `POST /actions/queue-remove`: a queued pull
request jumped to the front of the merge queue, or a hand pull request
removed from it, each recorded before GitHub is asked.

GitHub is faked at `github._call`, below the GraphQL error translation;
`origin` is a real git remote naming a GitHub URL.

Run: python3 -m unittest discover -s tests -p 'test_serve_queue_actions.py' -v
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_serve  # noqa: E402 - after the insert; TokenTests' TOKEN and BEARER
from schema_fixture import without_actions  # noqa: E402 - after the insert
from serve_fixture import ServeTestCase  # noqa: E402 - after the insert

import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.read  # noqa: E402 - after the sys.path insert above
from tests.host_fixture import git  # noqa: E402

REPO = "https://github.com/example/repo"
QUEUED = (49, 50, 51, 52)
REFUSAL = ("Pull request #52 cannot jump the merge queue: the actor is not"
           " allowed to bypass the ruleset")


def url(number):
    return f"{REPO}/pull/{number}"


def head(number):
    return f"{number:040x}"


class FakeGithub:
    """The queue read, the pull request read and the two queue mutations,
    as GitHub's API answers them."""

    def __init__(self):
        self.reads, self.mutations, self.refusal = 0, [], None

    def call(self, target, host, method, path, payload, accept=None):
        if path != "graphql":
            number = int(path.removeprefix("repos/example/repo/pulls/"))
            return {"number": number, "node_id": f"PR_node{number}"}
        query, variables = payload["query"], payload["variables"]
        for name in ("enqueuePullRequest", "dequeuePullRequest"):
            if name in query:
                self.mutations.append((name, variables))
                if self.refusal is not None:
                    return {"data": {name: None},
                            "errors": [{"type": "UNPROCESSABLE",
                                        "message": self.refusal}]}
                return {"data": {name: {"mergeQueueEntry": None}}}
        self.reads += 1
        done = {"nodes": [], "pageInfo": {"hasNextPage": False}}
        entries = [{"position": position, "state": "QUEUED",
                    "enqueuedAt": "2026-11-04T10:00:00Z",
                    "estimatedTimeToMerge": 600, "jump": False, "solo": False,
                    "pullRequest": {"number": number, "title": f"pr {number}",
                                    "url": url(number), "headRefOid": head(number),
                                    "author": {"login": "maintainer"}}}
                   for position, number in enumerate(QUEUED, start=1)]
        return {"data": {"repository": {
            "mergeQueue": {"configuration": {},
                           "entries": {**done, "nodes": entries}},
            "pullRequests": done}}}


class QueueActionCase(ServeTestCase):
    TOKEN = test_serve.TokenTests.TOKEN
    BEARER = test_serve.TokenTests.BEARER

    def setUp(self):
        super().setUp()
        self.seed()
        git(self.target, "init", "-q", "-b", "main")
        git(self.target, "remote", "add", "origin", REPO + ".git")
        conn = store.open(str(self.db))
        try:
            store.set_pull_request(conn, self.run, url(50))
        finally:
            conn.close()
        self.github = FakeGithub()
        self.enterContext(patch.object(holophyte.pr.github, "_call",
                                       self.github.call))

    def start_actions(self, on=True):
        path = self.root / "serve.token"
        path.write_text(self.TOKEN + "\n")
        path.chmod(0o600)
        self.start(f'[serve]\ntoken_file = "{path}"\n'
                   + ("actions = true\n" if on else ""))

    def post(self, action, headers=None, **body):
        return self.request("POST", f"/actions/{action}",
                            self.BEARER if headers is None else headers,
                            body=body)

    def rows(self):
        conn = store.read.open_readonly(self.db)
        try:
            return conn.execute(
                "SELECT id, runId, projectId, action, note FROM interventions"
                " WHERE action != 'migrate' ORDER BY id").fetchall()
        finally:
            conn.close()

    def dump(self):
        conn = store.read.open_readonly(self.db)
        try:
            return list(conn.iterdump())
        finally:
            conn.close()


class QueueJumpTests(QueueActionCase):
    def setUp(self):
        super().setUp()
        self.start_actions()

    def test_a_hand_pull_request_jumps_recorded_on_the_project(self):
        code, _, _ = self.request("GET", "/merge-queue", self.BEARER)
        self.assertEqual(code, 200)

        code, _, body = self.post("queue-jump", pr=52, note="hotfix")

        self.assertEqual(code, 200, body)
        self.assertEqual(self.github.mutations, [
            ("enqueuePullRequest",
             {"pull": "PR_node52", "sha": head(52), "jump": True})])
        self.assertEqual((body["action"], body["ok"], body["pr"]),
                         ("queue-jump", True, 52))
        ((row_id, run_id, project_id, action, note),) = self.rows()
        self.assertEqual((body["recorded"], run_id, action),
                         (row_id, None, "queue_jump"))
        self.assertIsNotNone(project_id)
        self.assertTrue(note.endswith("hotfix"), note)
        self.assertEqual(self.github.reads, 2)
        self.request("GET", "/merge-queue", self.BEARER)
        self.assertEqual(self.github.reads, 3)

    def test_a_pull_request_a_run_carries_is_recorded_on_that_run(self):
        code, _, body = self.post("queue-jump", pr=50)

        self.assertIs(body["ok"], True, body)
        self.assertEqual([(row[0], row[1], row[2], row[3])
                          for row in self.rows()],
                         [(body["recorded"], self.run, None, "queue_jump")])

    def test_a_github_refusal_answers_its_message_and_the_row_written_first(
            self):
        self.github.refusal = REFUSAL

        code, _, body = self.post("queue-jump", pr=52)

        self.assertEqual(code, 200)
        self.assertEqual((body["ok"], body["reason"], body["detail"]),
                         (False, "refused", REFUSAL))
        self.assertEqual(len(self.github.mutations), 1)
        self.assertEqual([(row[0], row[3]) for row in self.rows()],
                         [(body["recorded"], "queue_jump")])

    def test_the_front_entry_or_one_not_queued_is_refused_sending_nothing(self):
        before = self.dump()
        for number, reason in ((49, "at_front"), (77, "not_queued")):
            with self.subTest(pr=number):
                code, _, body = self.post("queue-jump", pr=number)
                self.assertEqual(code, 200)
                self.assertEqual((body["ok"], body["reason"], body["recorded"]),
                                 (False, reason, None))
        self.assertEqual(self.github.mutations, [])
        self.assertEqual(self.dump(), before)


class QueueRemoveTests(QueueActionCase):
    def setUp(self):
        super().setUp()
        self.start_actions()

    def test_a_hand_pull_request_is_dequeued_recorded_on_the_project(self):
        code, _, body = self.post("queue-remove", pr=51, note="wrong base")

        self.assertIs(body["ok"], True, body)
        self.assertEqual(self.github.mutations,
                         [("dequeuePullRequest", {"pull": "PR_node51"})])
        ((row_id, run_id, project_id, action, note),) = self.rows()
        self.assertEqual((body["recorded"], run_id, action),
                         (row_id, None, "queue_remove"))
        self.assertIsNotNone(project_id)

    def test_a_pull_request_a_run_carries_is_refused_naming_the_run(self):
        before = self.dump()

        code, _, body = self.post("queue-remove", pr=50)

        self.assertEqual(code, 200)
        self.assertEqual((body["ok"], body["reason"], body["recorded"]),
                         (False, "factory_owned", None))
        self.assertIn(f"run {self.run}", body["detail"])
        self.assertIn("KO-7", body["detail"])
        self.assertEqual(self.github.mutations, [])
        self.assertEqual(self.dump(), before)


class QueueActionGateTests(QueueActionCase):
    def test_both_routes_are_404_with_actions_off(self):
        self.start_actions(on=False)
        for action in ("queue-jump", "queue-remove"):
            with self.subTest(action=action):
                code, _, _ = self.post(action, pr=52)
                self.assertEqual(code, 404)

    def test_without_the_token_401_and_a_bad_pr_400_sending_nothing(self):
        self.start_actions()
        before = self.dump()
        for action in ("queue-jump", "queue-remove"):
            with self.subTest(action=action):
                code, _, _ = self.post(action, headers={}, pr=52)
                self.assertEqual(code, 401)
                for bad in ("52", 0, -3, True, None, 52.0):
                    code, _, body = self.post(action, pr=bad)
                    self.assertEqual(code, 400, (bad, body))
        self.assertEqual(self.github.mutations, [])
        self.assertEqual(self.dump(), before)


class QueueActionSchemaTests(QueueActionCase):
    def test_a_store_at_the_previous_schema_accepts_both_actions(self):
        conn = store.open(str(self.db))
        try:
            store.record_project_intervention(conn, "hold", "maintenance")
            store.record_intervention(conn, self.run, "pause", "lunch")
            before = conn.execute("SELECT * FROM interventions"
                                  " WHERE action != 'migrate'").fetchall()
        finally:
            conn.close()
        without_actions(self.db, 66, "queue_jump", "queue_remove")
        raw = sqlite3.connect(self.db)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK"):
                raw.execute("INSERT INTO interventions (projectId, source,"
                            ' "trigger", action, note, at)'
                            " VALUES (1, 'human', 'manual', 'queue_jump',"
                            " 'n', 1)")
        finally:
            raw.close()

        conn = store.open(str(self.db))
        self.addCleanup(conn.close)

        self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                         (store.SCHEMA_VERSION,))
        self.assertEqual(conn.execute("SELECT * FROM interventions"
                                      " WHERE action != 'migrate'").fetchall(),
                         before)
        store.record_project_intervention(conn, "queue_jump", "jump #52")
        store.record_intervention(conn, self.run, "queue_remove", "remove #50")


if __name__ == "__main__":
    import unittest
    unittest.main()
