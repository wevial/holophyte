"""`holo queue` prints a project's merge queue and `holo queue jump` and
`holo queue remove` act on it, over `transport = "http"` against a real host
daemon and locally through the handler that daemon's actions call.

The daemon runs in this process with GitHub faked at `github.graphql` and
`github.rest`; a local `holo` reaches the same fake through a `gh`
executable on PATH answering as `gh api` does. `holo` runs as its own process.

Run: python3 -m unittest discover -s tests -p 'test_holo_queue.py' -v
"""
import contextlib
import http.client
import io
import json
import subprocess
import sys

import holophyte.cli.entry
import store
import store.tickets
from holophyte.config.project import Project
from holophyte.loop.gates import InfraFailure
from holophyte.pr.merge_queue import GRAPHQL_REFUSED
from tests.test_holo_http import MACHINE, ROOT, HttpCase
from tests.test_serve_merge_queue import (
    FACTORY_REMOVED,
    HAND_REMOVED,
    REPO,
    FakeGithub,
    entry,
    url,
)

REFUSAL = ("Pull request #2552 cannot jump the merge queue: the actor is not"
           " allowed to bypass the ruleset")
FAKE_GH = '''#!{python}
import json, sys
sys.path.insert(0, {root!r})
from tests.test_holo_queue import QueueGithub
argv = sys.argv[1:]
method, path = argv[argv.index("--method") + 1], argv[-1]
if "--input" in argv:
    path = argv[argv.index("--input") - 1]
fake = QueueGithub()
fake.refusal = {refusal!r}
if path == "graphql":
    payload = json.load(sys.stdin)
    try:
        print(json.dumps({{"data": fake.graphql(None, None, payload["query"],
                                                payload["variables"])}}))
    except Exception:
        print(json.dumps({{"errors": [{{"message": fake.refusal}}]}}))
else:
    print(json.dumps(fake.rest(None, None, method, path)))
'''


class QueueGithub(FakeGithub):
    """The four-entry queue with the pull request at position 2 a store run
    carries, one removal and one merge in the last two hours, and the two
    queue mutations."""

    def __init__(self):
        super().__init__()
        self.queue["entries"]["nodes"] = [
            entry(3, 2551, "QUEUED"), entry(1, 2547, "AWAITING_CHECKS"),
            entry(4, 2552, "QUEUED"), entry(2, 2549, "QUEUED")]
        self.pulls = [pull for pull in self.pulls
                      if pull["number"] != HAND_REMOVED]
        self.mutations, self.refusal = [], None

    def graphql(self, target, pull, query, variables):
        for name in ("enqueuePullRequest", "dequeuePullRequest"):
            if name in query:
                self.mutations.append((name, variables))
                if self.refusal is not None:
                    raise InfraFailure(GRAPHQL_REFUSED + self.refusal)
                return {name: {"mergeQueueEntry": None}}
        return super().graphql(target, pull, query, variables)

    def rest(self, target, pull, method, path, payload=None):
        if path.startswith("repos/example/repo/pulls/"):
            number = int(path.rsplit("/", 1)[1])
            return {"number": number, "node_id": f"PR_node{number}"}
        return super().rest(target, pull, method, path, payload)


class QueueCase(HttpCase):
    def setUp(self):
        super().setUp()
        self.github = QueueGithub()
        self.github.install(self)
        (self.seat / "client.toml").write_text(
            (self.seat / "client.toml").read_text()
            + 'timezone = "Asia/Kolkata"\n')

    def register(self, name):
        path = self.root / name
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)],
                       check=True)
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin",
                        REPO + ".git"], check=True)
        target = Project.locate(path, adopt=False)
        target.holo_dir.mkdir(parents=True, exist_ok=True)
        target.config_path.write_text(
            f'[board]\nkind = "native"\nprefix = "{name.upper()}"\n'
            f'[serve]\nname = "{name}"\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(holophyte.cli.entry.cli(["project", "add",
                                                      str(path)]))
        return path

    def serve(self):
        self.relos = self.register("relos")
        self.register("holophyte")
        conn = store.open(str(Project.locate(self.relos).store_path))
        try:
            project = store.tickets.ensure_project(conn, "native:RELOS",
                                                   self.relos)
            self.carried = self.carry(conn, project, "RELOS-227", url(2549))
            self.carry(conn, project, "RELOS-230", url(FACTORY_REMOVED))
        finally:
            conn.close()
        super().serve()

    def carry(self, conn, project, key, pull_url):
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id=f"issue-{key}",
            linear_identifier=key, title="a ticket",
            acceptance_criteria=["Given a ticket, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * 60_000)
        store.tickets.transition(conn, ticket, "in_flight")
        run = store.claim(conn, project, ticket)
        store.set_phase(conn, run, "working")
        store.set_pull_request(conn, run, pull_url)
        return run

    def interventions(self):
        conn = store.open(str(Project.locate(self.relos).store_path))
        try:
            return conn.execute(
                "SELECT runId, action, note FROM interventions"
                " WHERE action != 'migrate' ORDER BY id").fetchall()
        finally:
            conn.close()

    def install_gh(self, refusal=None):
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH.format(python=sys.executable, root=str(ROOT),
                                     refusal=refusal))
        gh.chmod(0o755)

    def newest_intervention(self):
        conn = store.open(str(Project.locate(self.relos).store_path))
        try:
            return conn.execute("SELECT MAX(id) FROM interventions").fetchone()[0]
        finally:
            conn.close()

    def route(self, path):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            connection.request("GET", path,
                               headers={"Authorization": f"Bearer {MACHINE}"})
            return json.loads(connection.getresponse().read())
        finally:
            connection.close()


class QueueReadTests(QueueCase):
    def test_queue_prints_its_configuration_entries_and_exits_newest_first(self):
        completed = self.holo("queue", "-p", "relos")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.splitlines(), [
            "main · squash · all green · builds 1 at a time"
            " · checks time out at 60m",
            "1 #2547 checks running 5/13 ~10m · maintainer",
            "2 #2549 queued ~20m · RELOS-227",
            "3 #2551 queued ~30m · maintainer",
            "4 #2552 queued ~40m · maintainer",
            "#2561 removed 19:56: failed checks · RELOS-230",
            "#2562 merged 19:36 · maintainer"])

    def test_queue_json_prints_the_routes_body_unchanged(self):
        completed = self.holo("queue", "--json", "-p", "relos")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout),
                         self.route("/projects/relos/merge-queue"))

    def test_a_branch_with_no_merge_queue_says_so_and_exits_zero(self):
        self.github.queue = None
        completed = self.holo("queue", "-p", "holophyte")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "no merge queue on main\n")


class QueueWriteTests(QueueCase):
    def posts(self, action):
        return [request for request in self.requests
                if request == ("POST", f"/projects/relos/actions/{action}")]

    def test_jump_posts_the_pull_request_and_note_and_prints_the_detail(self):
        before = self.interventions()
        completed = self.holo("queue", "jump", "2552", "hotfix", "-p", "relos")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self.posts("queue-jump")), 1)
        self.assertEqual(self.github.mutations, [
            ("enqueuePullRequest", {"pull": "PR_node2552",
                                    "sha": f"{2552:040d}", "jump": True})])
        ((run, action, note),) = self.interventions()[len(before):]
        self.assertEqual((run, action), (None, "queue_jump"))
        self.assertTrue(note.endswith("jump #2552; hotfix"), note)
        self.assertIn("#2552 jumped to the front of the merge queue",
                      completed.stdout)

    def test_a_github_refusal_prints_its_message_and_exits_one(self):
        self.github.refusal = REFUSAL
        completed = self.holo("queue", "jump", "2552", "hotfix", "-p", "relos")
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertIn(REFUSAL, completed.stderr)
        self.assertNotIn(GRAPHQL_REFUSED, completed.stderr)

    def test_a_refused_jump_reports_its_recorded_row_over_http_and_locally(self):
        self.github.refusal = REFUSAL
        self.install_gh(REFUSAL)
        for name, home in (("http", None), ("local", self.host)):
            with self.subTest(transport=name):
                completed = self.holo("queue", "jump", "2552", "--json",
                                      "-p", "relos", home=home)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertFalse(result["ok"])
                self.assertIn(REFUSAL, result["detail"])
                self.assertEqual(result["recorded"], self.newest_intervention())
                self.assertEqual(self.interventions()[-1][1], "queue_jump")

    def test_removing_a_runs_pull_request_is_refused_over_http_and_locally(self):
        refusal = (f"#2549 is run {self.carried}'s pull request (RELOS-227);"
                   " pause or abort the run instead")
        before = self.interventions()
        over_http = self.holo("queue", "remove", "2549", "-p", "relos")
        self.assertEqual(self.github.mutations, [])
        self.install_gh()
        local = self.holo("queue", "remove", "2549", "-p", "relos",
                          home=self.host)
        for name, completed in (("http", over_http), ("local", local)):
            with self.subTest(transport=name):
                self.assertEqual(completed.returncode, 1, completed.stdout)
                self.assertIn(refusal, completed.stderr)
        self.assertNotIn("via http", local.stderr)
        self.assertEqual(len(self.posts("queue-remove")), 1)
        self.assertEqual(self.interventions(), before)


if __name__ == "__main__":
    import unittest
    unittest.main()
