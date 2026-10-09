"""`POST /actions/...` (`holophyte.serve.serve_actions`): the unit actions and
the requeue action behind the token, with their interventions rows.

Run: python3 -m unittest discover -s tests -p 'test_serve_actions*' -v
"""
from __future__ import annotations

import io
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_serve  # noqa: E402 - after the insert; TokenTests' TOKEN and BEARER
from serve_action_fixture import UnitActionCases  # noqa: E402
from serve_fixture import MIN, ServeTestCase  # noqa: E402 - after the insert

import holophyte.config.project  # noqa: E402 - after the sys.path insert above
import holophyte.serve.server  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.read  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above


class ActionsTests(UnitActionCases, ServeTestCase):
    """`POST /actions/...` (KO-348): 404 on every daemon without `[serve]
    actions = true`; with it, the unit actions run `systemctl --user`
    against the `[serve] name` instance behind the token -- on a loopback
    bind as much as any other -- after their interventions row, a failed
    `systemctl` is `ok: false` carrying its stderr, and `requeue` is the
    store's own requeue with its interventions row."""

    TOKEN = test_serve.TokenTests.TOKEN
    BEARER = test_serve.TokenTests.BEARER

    def token_config(self, extra=""):
        path = self.root / "serve.token"
        path.write_text(self.TOKEN + "\n")
        path.chmod(0o600)
        return f'[serve]\ntoken_file = "{path}"\n{extra}'

    def test_send_back_records_note_and_refuses_without_writes(self):
        self.seed()
        self.start(self.token_config('actions = true\n'))
        def send(note):
            return self.request("POST", "/actions/send-back", self.BEARER,
                                body={"run": self.run, "note": note,
                                      "author": "maintainer"})
        with store.open(str(self.db)) as conn:
            before = conn.execute("SELECT COUNT(*) FROM runEvents").fetchone()
        self.assertFalse(send("remove the subheader")[2].get("ok", False))
        with store.open(str(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runEvents").fetchone(),
                             before)
            for phase in ("verifying", "reviewing", "merge_gate"):
                store.set_phase(conn, self.run, phase)
            store.park(conn, self.run, "awaiting_merge_approval",
                       pr_url="https://example.test/org/repo/pull/1")
            store.tickets.transition(conn, 1, "blocked_on_operator")
            before = conn.execute("SELECT COUNT(*) FROM runEvents").fetchone()
        self.assertFalse(send("   ")[2].get("ok", False))
        with store.open(str(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runEvents").fetchone(),
                             before)
        code, _, result = send("remove the subheader")
        self.assertEqual(code, 200)
        self.assertTrue(result["ok"], result)
        with store.open(str(self.db)) as conn:
            import json
            guidance, = conn.execute(
                "SELECT guidance FROM interventions"
                " WHERE action = 'operator_note'").fetchone()
            self.assertEqual(json.loads(guidance),
                             {"note": "remove the subheader", "author": "maintainer"})
            payload, = conn.execute(
                "SELECT payload FROM runEvents WHERE kind = 'operator_note'").fetchone()
            self.assertEqual(json.loads(payload)["author"], "maintainer")
            self.assertEqual(conn.execute("SELECT status FROM tickets").fetchone(),
                             ("ready",))
            self.assertEqual(conn.execute("SELECT outcome FROM runs").fetchone(),
                             ("abandoned",))

    def completed(self, argv, returncode=0, stderr=""):
        return subprocess.CompletedProcess(argv, returncode, stdout="",
                                           stderr=stderr)

    def test_without_the_opt_in_every_actions_route_is_404_with_the_token(self):
        self.seed()
        self.start(self.token_config(), host="0.0.0.0")
        with patch.object(subprocess, "run") as run:
            for action in ("restart-supervisor", "launch-loop", "requeue", "send-back"):
                with self.subTest(action=action):
                    code, _, body = self.request(
                        "POST", f"/actions/{action}", self.BEARER,
                        body={"ticket": "KO-7"})
                    self.assertEqual(code, 404)
                    self.assertEqual(body["error"], "not found")
        run.assert_not_called()
        conn = store.read.open_readonly(self.db)
        try:
            self.assertEqual(store.read.ledger(conn, self.run), [])
        finally:
            conn.close()

    def test_a_preflight_for_an_action_grants_the_post_and_its_json_body(self):
        # The console on another daemon's page asks before posting an
        # action with the bearer and a JSON Content-Type; a preflight
        # that named only GET would have the browser block the click.
        self.seed()
        self.start(self.token_config("actions = true\n"), host="0.0.0.0")

        with patch.object(store.read, "open_readonly") as opened:
            code, headers, raw = self.fetch(
                "OPTIONS", "/actions/restart-supervisor",
                {"Origin": "http://page.example:7710",
                 "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers":
                     "authorization, content-type"})
        self.assertEqual(code, 204)
        self.assertEqual(raw, b"")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(headers["Access-Control-Allow-Methods"],
                         "GET, POST, PUT")
        allowed = headers["Access-Control-Allow-Headers"].lower()
        self.assertIn("authorization", allowed)
        self.assertIn("content-type", allowed)
        opened.assert_not_called()

    def test_status_advertises_the_opt_in(self):
        # KO-349: the console reads `actions` before a click, so a daemon
        # without the routes draws its buttons disabled instead of
        # posting into a 404.
        self.seed()
        self.start(self.token_config('actions = true\nname = "writer-a"\n'))
        code, _, body = self.request("GET", "/status")
        self.assertEqual(code, 200)
        self.assertIs(body["actions"], True)
        # KO-358: the settings sheet reads `config_edit` the same way and
        # draws itself read-only, naming the key, without it.
        self.assertIs(body["config_edit"], False)

    def test_a_loopback_bind_demands_the_token_for_actions(self):
        """The bind address guards reads, not the units: on loopback `/status`
        stays open while `POST /actions/...` is 401 without the bearer and
        runs nothing, then 200 with it."""
        self.seed()
        self.start(self.token_config('actions = true\nname = "writer-a"\n'))
        code, _, body = self.request("GET", "/status")
        self.assertEqual(code, 200)
        self.assertIn("runs", body)
        with patch.object(subprocess, "run") as run:
            for headers in (None, {"Authorization": "Bearer wrong"}):
                with self.subTest(headers=headers):
                    code, _, body = self.request(
                        "POST", "/actions/restart-supervisor", headers)
                    self.assertEqual(code, 401)
                    self.assertEqual(body, {})
            run.assert_not_called()
            run.side_effect = lambda argv, **kw: self.completed(argv)
            code, _, body = self.request("POST", "/actions/restart-supervisor",
                                         self.BEARER)
        self.assertEqual(code, 200)
        self.assertIs(body["ok"], True)
        self.assertEqual(run.call_args.args[0],
                         ["systemctl", "--user", "restart",
                          "holophyte-supervise@writer-a"])
        conn = store.read.open_readonly(self.db)
        try:
            (count,) = conn.execute(
                'SELECT COUNT(*) FROM interventions'
                " WHERE action != 'migrate'").fetchone()
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_actions_without_a_token_file_are_a_startup_error_on_loopback(self):
        self.seed()
        (self.db.parent / "config.toml").write_text("[serve]\nactions = true\n")
        project = holophyte.config.project.Project.locate(self.target)
        with self.assertRaises(SystemExit) as raised:
            holophyte.serve.server.serve(project, "127.0.0.1:0", out=io.StringIO())
        message = str(raised.exception)
        self.assertIn("[serve] token_file", message)
        self.assertIn("actions", message)

    def test_a_unit_action_with_no_run_to_record_against_does_not_run(self):
        """A store with no run has no row to hang the intervention on, and
        record-before-acting means the unit is left alone: `ok: false`
        naming why, `systemctl` never called. A target with no store is the
        same answer."""
        conn = store.open(str(self.db))
        try:
            store.init(conn)
        finally:
            conn.close()
        self.start(self.token_config("actions = true\n"))
        with patch.object(subprocess, "run") as run:
            code, _, body = self.request("POST", "/actions/launch-loop",
                                         self.BEARER)
            run.assert_not_called()
        self.assertEqual(code, 200)
        self.assertIs(body["ok"], False)
        self.assertIsNone(body["recorded"])
        self.assertIn("no run to record", body["detail"])
        # The same config, a second daemon, and no store at all.
        self.db.unlink()
        self.start()
        with patch.object(subprocess, "run") as run:
            code, _, body = self.request("POST", "/actions/launch-loop",
                                         self.BEARER)
            run.assert_not_called()
        self.assertEqual(code, 200)
        self.assertIs(body["ok"], False)

    def test_with_a_project_row_and_no_run_only_launch_loop_is_recorded_and_run(self):
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            store.tickets.ensure_project(conn, "team-1", self.target)
        finally:
            conn.close()
        self.start(self.token_config("actions = true\n"))
        with patch.object(subprocess, "run") as run:
            run.side_effect = lambda argv, **kw: self.completed(argv)
            _, _, restart = self.request("POST", "/actions/restart-supervisor",
                                         self.BEARER)
            run.assert_not_called()
            _, _, launch = self.request("POST", "/actions/launch-loop",
                                        self.BEARER)
        self.assertIs(restart["ok"], False)
        self.assertIn("no run to record", restart["detail"])
        self.assertIs(launch["ok"], True)
        self.assertEqual(run.call_args.args[0][:3], ["systemctl", "--user", "start"])
        conn = store.read.open_readonly(self.db)
        try:
            rows = conn.execute(
                'SELECT runId, "action" FROM interventions'
                " WHERE action != 'migrate'").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(None, "launch_loop")])

    def test_launch_loop_reports_a_failed_systemctl_as_ok_false(self):
        self.seed()
        self.start(self.token_config("actions = true\n"))
        stderr = "Failed to start holophyte-loop@repo.service: Unit not found."
        with patch.object(subprocess, "run") as run:
            run.side_effect = lambda argv, **kw: self.completed(
                argv, returncode=1, stderr=stderr + "\n")
            code, _, body = self.request("POST", "/actions/launch-loop",
                                         self.BEARER)
        self.assertEqual(code, 200)
        self.assertEqual(body["action"], "launch-loop")
        self.assertIs(body["ok"], False)
        self.assertIn(stderr, body["detail"])
        # The instance defaults to the target directory's name.
        self.assertEqual(run.call_args.args[0],
                         ["systemctl", "--user", "start", "holophyte-loop@repo"])

    def test_requeue_walks_a_failed_ticket_to_ready_with_its_intervention(self):
        self.seed_ended()
        self.start(self.token_config("actions = true\n"), host="0.0.0.0")
        code, _, body = self.request("POST", "/actions/requeue", self.BEARER,
                                     body={"ticket": "KO-2",
                                           "note": "the verify was flaky"})
        self.assertEqual(code, 200)
        self.assertEqual(body["action"], "requeue")
        self.assertIs(body["ok"], True)
        conn = store.read.open_readonly(self.db)
        try:
            ticket = store.read.ticket_by_identifier(conn, "KO-2")
            entries = store.read.ledger_since(conn, 0, kind="intervention",
                                              ticket="KO-2")
        finally:
            conn.close()
        self.assertEqual(ticket.status, "ready")
        self.assertEqual(len(entries), 1)
        self.assertIn("requeue", entries[0].text)
        self.assertIn("the verify was flaky", entries[0].text)
        self.assertEqual(entries[0].source, "operator")

        # A merged ticket is refused by the store, and that is `ok: false`
        # with the refusal, not a 500; an unmirrored one the same.
        for ticket_id, fragment in (("KO-1", "merged"), ("KO-99", "no such")):
            with self.subTest(ticket=ticket_id):
                code, _, body = self.request("POST", "/actions/requeue",
                                             self.BEARER,
                                             body={"ticket": ticket_id})
                self.assertEqual(code, 200)
                self.assertIs(body["ok"], False)
                self.assertIn(fragment, body["detail"])
        code, _, body = self.request("POST", "/actions/requeue", self.BEARER,
                                     body={})
        self.assertEqual(code, 400)
        self.assertIn("ticket", body["error"])

    def test_requeue_refuses_an_identifier_the_store_holds_twice(self):
        # The CLI's `--requeue` refuses to pick one of two tickets named
        # alike; the route must refuse the same way, and neither may move.
        self.seed_ended()
        conn = store.open(str(self.db))
        try:
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            twin = store.tickets.mirror_ticket(
                conn, project, linear_issue_id="issue-KO-2-twin",
                linear_identifier="KO-2", title="ticket KO-2 again",
                acceptance_criteria=["Given KO-2, then it is worked"],
                verification_commands=["echo ok"], time_box_ms=20 * MIN)
            store.tickets.transition(conn, twin, "in_flight")
            run = store.claim(conn, project, twin, now=self.now - 30 * MIN)
            store.release(conn, run, "failed", now=self.now - 20 * MIN)
        finally:
            conn.close()

        def twins():
            conn = store.read.open_readonly(self.db)
            try:
                statuses = [row[0] for row in conn.execute(
                    "SELECT status FROM tickets WHERE linearIdentifier = 'KO-2'"
                    " ORDER BY id")]
                entries = store.read.ledger_since(conn, 0, kind="intervention",
                                                  ticket="KO-2")
            finally:
                conn.close()
            return statuses, entries
        before = twins()
        self.assertEqual(len(before[0]), 2)
        self.assertNotIn("ready", before[0])
        self.start(self.token_config("actions = true\n"), host="0.0.0.0")
        code, _, body = self.request("POST", "/actions/requeue", self.BEARER,
                                     body={"ticket": "KO-2", "note": "retry"})
        self.assertEqual(code, 200)
        self.assertIs(body["ok"], False)
        self.assertIn("2 tickets", body["detail"])
        after = twins()
        self.assertEqual(after, before)
        self.assertNotIn("ready", after[0])
        self.assertEqual(after[1], [])

    def interventions(self, action=None):
        conn = store.read.open_readonly(self.db)
        try:
            if action is None:
                return conn.execute(
                    "SELECT * FROM interventions ORDER BY id").fetchall()
            return [row for (row,) in conn.execute(
                "SELECT id FROM interventions WHERE action = ? ORDER BY id",
                (action,))]
        finally:
            conn.close()

    def test_a_unit_action_answers_the_id_of_the_row_it_wrote(self):
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            store.tickets.ensure_project(conn, "team-1", self.target)
        finally:
            conn.close()
        self.start(self.token_config("actions = true\n"))
        with patch.object(subprocess, "run") as run:
            run.side_effect = lambda argv, **kw: self.completed(argv)
            _, _, on_project = self.request("POST", "/actions/launch-loop",
                                            self.BEARER)
            self.seed()
            _, _, restart = self.request("POST", "/actions/restart-supervisor",
                                         self.BEARER)
            _, _, launch = self.request("POST", "/actions/launch-loop",
                                        self.BEARER)
        project_row, run_row = self.interventions("launch_loop")
        (restart_row,) = self.interventions("restart_supervisor")
        self.assertNotIn(self.run, (project_row, run_row, restart_row))
        self.assertEqual(
            [(body["ok"], body["recorded"])
             for body in (on_project, restart, launch)],
            [(True, project_row), (True, restart_row), (True, run_row)])

    def test_two_overlapping_restarts_each_answer_their_own_row(self):
        self.seed()
        self.start(self.token_config("actions = true\n"))
        first_running, second_ran = threading.Event(), threading.Event()

        def systemctl(argv, **kw):
            if first_running.is_set():
                second_ran.set()
            else:
                first_running.set()
                second_ran.wait(timeout=1)
            return self.completed(argv)
        replies = {}

        def restart(name):
            replies[name] = self.request(
                "POST", "/actions/restart-supervisor", self.BEARER)[2]
        with patch.object(subprocess, "run", side_effect=systemctl):
            first = threading.Thread(target=restart, args=("first",))
            first.start()
            first_running.wait(timeout=10)
            restart("second")
            first.join(timeout=10)
        rows = self.interventions("restart_supervisor")
        self.assertEqual(len(rows), 2)
        self.assertEqual((replies["first"]["recorded"],
                          replies["second"]["recorded"]), tuple(rows))

    def test_requeue_and_send_back_answer_the_row_each_wrote(self):
        self.seed()
        self.seed_ended()
        with store.open(str(self.db)) as conn:
            for phase in ("verifying", "reviewing", "merge_gate"):
                store.set_phase(conn, self.run, phase)
            store.park(conn, self.run, "awaiting_merge_approval",
                       pr_url="https://example.test/org/repo/pull/1")
            ticket = store.read.ticket_by_identifier(conn, "KO-7").id
            store.tickets.transition(conn, ticket, "blocked_on_operator")
        self.start(self.token_config("actions = true\n"))
        _, _, requeued = self.request("POST", "/actions/requeue", self.BEARER,
                                      body={"ticket": "KO-2"})
        _, _, sent = self.request("POST", "/actions/send-back", self.BEARER,
                                  body={"run": self.run, "note": "rename it"})
        (requeue_row,) = self.interventions("requeue")
        (note_row,) = self.interventions("operator_note")
        for body, action, row in ((requeued, "requeue", requeue_row),
                                  (sent, "send-back", note_row)):
            with self.subTest(action=action):
                self.assertEqual((body["action"], body["ok"], body["recorded"]),
                                 (action, True, row), body)
                self.assertTrue(body["detail"])

    def test_a_refused_action_answers_recorded_null_and_writes_no_row(self):
        self.seed()
        with store.open(str(self.db)) as conn:
            store.release(conn, self.run, "failed", reason="verify red")
            ticket = store.read.ticket_by_identifier(conn, "KO-7").id
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            newer = store.claim(conn, project, ticket, now=self.now)
        self.assertNotEqual(newer, self.run)
        self.start(self.token_config("actions = true\n"))
        before = self.interventions()
        refusals = (("requeue", {"ticket": "KO-99"}),
                    ("send-back", {"run": self.run, "note": "rename it"}),
                    ("pause", {"run": self.run, "note": "reboot"}))
        for action, body in refusals:
            with self.subTest(action=action):
                code, _, reply = self.request("POST", f"/actions/{action}",
                                              self.BEARER, body=body)
                self.assertEqual(code, 200, reply)
                self.assertEqual(
                    (reply["action"], reply["ok"], reply["recorded"]),
                    (action, False, None), reply)
                self.assertTrue(reply["detail"])
        self.assertEqual(self.interventions(), before)


if __name__ == "__main__":
    unittest.main()
