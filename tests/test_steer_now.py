"""`holo steer KEY --now`: the running implementer turn is stopped, its edits
kept as WIP, and the same session resumed with the note.

Run: python3 -m unittest discover -s tests -p 'test_steer_now.py' -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import threading
import time
import uuid
from unittest.mock import patch

import holophyte.loop.implement as implement
import store
from holophyte.agents.agent_routes import routes
from holophyte.agents.fix_session import steer_turn
from holophyte.agents.roles import agent
from holophyte.holo import cli as holo_cli
from store import operator_notes, steer_notes
from tests.sweep_fixture import SweepTestCase

BEAT = 0.5
NOTE = "log the port too"
CODEX_SESSION = str(uuid.UUID(int=7))
STALL = ('echo "steered edit" > a.txt\n'
         'echo junk > scratch.tmp\n'
         'echo $$ > "$STATE/pid"\n'
         'touch "$STATE/ready"\n'
         'exec sleep 20\n')


class SteerNowTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        self.state = self.root / "state"
        self.state.mkdir()
        for args in (["init", "-q", "-b", "main"],
                     ["config", "user.name", "tester"],
                     ["config", "user.email", "tester@example.invalid"]):
            self.git(*args)
        (self.target / "a.txt").write_text("base\n")
        (self.target / ".gitignore").write_text("*.tmp\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.run_id = self.a_run()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.target, check=True,
                              capture_output=True, text=True).stdout.strip()

    def stand_in(self, name, *, first, later):
        """A real agent binary: its Nth launch logs its arguments as `argv-N`
        and runs `first` on launch 1, else `later`."""
        script = self.root / name
        script.write_text(
            "#!/bin/sh\n"
            f'STATE="{self.state}"\n'
            'n=$(( $(cat "$STATE/count" 2>/dev/null || echo 0) + 1 ))\n'
            'echo "$n" > "$STATE/count"\n'
            'printf "%s\\0" "$@" > "$STATE/argv-$n"\n'
            f'if [ "$n" = 1 ]; then\n{first}fi\n{later}')
        script.chmod(0o755)
        return script

    def claude(self, first=STALL):
        script = self.stand_in("claude", first=first,
                               later="echo '{\"result\": \"resumed\"}'\n")
        self.configure(f'[harnesses]\nclaude = "{script}"\n'
                       '[agents.implementer]\nharness = "claude"\n'
                       'model = "m1"\neffort = "high"\n'
                       '[supervisor]\nheartbeat_stale_min = 0.0167\n')

    def codex(self, banner):
        first = (f'echo "session id: {CODEX_SESSION}"\n' if banner else "") + STALL
        script = self.stand_in("codex", first=first, later="echo resumed\n")
        self.configure(f'[harnesses]\ncodex = "{script}"\n'
                       '[agents.implementer]\nharness = "codex"\n'
                       'model = "m1"\neffort = "high"\n')

    def argv(self, n):
        return (self.state / f"argv-{n}").read_text().split("\0")[:-1]

    def launches(self):
        return int((self.state / "count").read_text())

    def holo(self, *words):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = holo_cli.main([*words, "-p", str(self.target)]) or 0
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue()

    def when_ready(self, act):
        """Run `act` on another thread once the first launch is running, and
        time how long its process group outlives it."""
        timing = {}

        def watch():
            ready = self.state / "ready"
            deadline = time.monotonic() + 15
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            pid = int((self.state / "pid").read_text())
            timing["said"] = act()
            noted = time.monotonic()
            while time.monotonic() - noted < 15:
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    timing["gone"] = time.monotonic() - noted
                    return
                time.sleep(0.02)

        thread = threading.Thread(target=watch)
        thread.start()
        self.addCleanup(thread.join)
        return thread, timing

    def steer_now_by_cli(self):
        return self.holo("steer", "KO-1", "--now", "-n", NOTE)

    def interrupt_in_store(self):
        conn = store.open(str(self.db))
        try:
            steer_notes.steer(conn, self.ticket_of[self.run_id], NOTE, "ko",
                              interrupt=True)
        finally:
            conn.close()

    def timed(self, seconds=None):
        return implement._timed(self.project, self.conn, self.run_id, BEAT,
                                self.target, 25, "Implement the goal.",
                                seconds=seconds)

    def events(self, kind):
        return self.conn.execute(
            "SELECT summary, payload FROM runEvents WHERE runId = ? AND kind = ?",
            (self.run_id, kind)).fetchall()

    def notes(self):
        return self.conn.execute(
            "SELECT interrupt, consumedBy FROM steerNotes").fetchall()

    def test_a_steer_now_stops_the_turn_keeps_its_edit_and_drops_its_ignored_file(self):
        self.claude()
        thread, timing = self.when_ready(self.steer_now_by_cli)

        output, timed_out = self.timed()
        thread.join()

        code, said = timing["said"]
        self.assertEqual(code, 0, said)
        self.assertLessEqual(timing["gone"], 2 * BEAT + 0.5)
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         "WIP: implementer steered mid-edit (KO-1); not verified")
        self.assertEqual(self.git("show", "HEAD:a.txt"), "steered edit")
        self.assertFalse((self.target / "scratch.tmp").exists())
        self.assertEqual(len(self.events("steer_interrupt")), 1)
        self.assertEqual(self.conn.execute(
            "SELECT phase, endedAt FROM runs WHERE id = ?",
            (self.run_id,)).fetchone(), ("working", None))
        self.assertEqual((str(output), timed_out), ("resumed", False))

    def test_the_resumed_turn_resumes_the_claude_session_with_the_note(self):
        self.claude()
        thread, _ = self.when_ready(self.steer_now_by_cli)

        self.timed()
        thread.join()

        first = self.argv(1)
        session = first[first.index("--session-id") + 1]
        *resumed, prompt = self.argv(2)
        self.assertEqual(resumed, ["-p", "--resume", session, "--model", "m1",
                                   "--effort", "high", "--output-format", "json"])
        self.assertIn(NOTE, prompt)
        self.assertNotIn("Implement the goal.", prompt)
        self.assertEqual(self.notes(), [(1, self.run_id)])

    def test_the_resumed_turn_is_armed_with_what_the_stopped_turn_had_left(self):
        self.claude()
        thread, _ = self.when_ready(self.steer_now_by_cli)
        armed = []
        real = implement.agent

        def spy(*args, **kwargs):
            armed.append(kwargs["timeout"])
            return real(*args, **kwargs)

        with patch.object(implement, "agent", side_effect=spy):
            self.timed(seconds=300)
        thread.join()

        first, resumed = armed
        self.assertEqual(first, 300)
        self.assertLess(resumed, 300)
        self.assertGreater(resumed, 290)

    def test_a_codex_turn_resumes_the_session_its_banner_named(self):
        self.codex(banner=True)
        thread, _ = self.when_ready(self.interrupt_in_store)

        self.timed()
        thread.join()

        self.assertEqual(self.argv(2)[:3], ["exec", "resume", CODEX_SESSION])
        self.assertIn(NOTE, self.argv(2)[-1])

    def test_a_codex_turn_stopped_before_its_banner_restarts_fresh_naming_the_wip(self):
        self.codex(banner=False)
        store.record_agent_session(self.conn, self.run_id, str(uuid.UUID(int=3)),
                                   "implement", "primary")
        thread, _ = self.when_ready(self.interrupt_in_store)

        self.timed()
        thread.join()

        fresh = self.argv(2)
        self.assertNotEqual(fresh[1], "resume")
        wip = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         "WIP: implementer steered mid-edit (KO-1); not verified")
        self.assertIn(wip[:12], fresh[-1])
        self.assertIn(NOTE, fresh[-1])
        self.assertIn("Implement the goal.", fresh[-1])
        ((_, payload),) = self.events("steer_resumed")
        self.assertEqual(json.loads(payload)["reason"],
                         "the stopped turn reported no session")

    def test_a_babysit_fix_turn_is_stopped_and_resumed_carrying_the_note(self):
        self.claude()
        for phase in ("verifying", "reviewing", "merge_gate"):
            store.set_phase(self.conn, self.run_id, phase)
        store.set_pull_request(self.conn, self.run_id,
                               "https://github.com/example/repo/pull/7")
        thread, timing = self.when_ready(self.steer_now_by_cli)

        output, timed_out = self.timed()
        thread.join()

        code, said = timing["said"]
        self.assertEqual(code, 0, said)
        self.assertLessEqual(timing["gone"], 2 * BEAT + 0.5)
        self.assertEqual((str(output), timed_out), ("resumed", False))
        self.assertEqual(self.argv(2)[1], "--resume")
        self.assertIn(NOTE, self.argv(2)[-1])
        self.assertEqual(self.notes(), [(1, self.run_id)])
        self.assertEqual(operator_notes.notes(self.conn, self.run_id,
                                              pending=True), [])

    def test_a_command_implementer_reporting_its_recorded_session_resumes_it(self):
        script = self.stand_in("implementer",
                               first='echo "session id: s-1"\n' + STALL,
                               later="echo resumed\n")
        self.configure(f'[agents]\nimplementer = "{script}"\n'
                       "implementer_session = 'session id: (\\S+)'\n"
                       f'implementer_resume = "{script} --resume {{session}}"\n')
        store.record_agent_session(self.conn, self.run_id, "s-1", "implement",
                                   "primary")
        thread, timing = self.when_ready(self.steer_now_by_cli)

        self.timed()
        thread.join()

        code, said = timing["said"]
        self.assertEqual(code, 0, said)
        self.assertEqual(self.argv(2)[:2], ["--resume", "s-1"])
        self.assertIn(NOTE, self.argv(2)[-1])

    def test_a_fallback_implementer_takes_the_note_without_stopping_the_turn(self):
        self.claude(first='touch "$STATE/ready"\necho $$ > "$STATE/pid"\n'
                          f'sleep {4 * BEAT}\necho finished\nexit 0\n')
        store.record_agent_session(self.conn, self.run_id, "primary-session",
                                   "implement", "primary")
        script = self.root / "claude"
        routes(self.project).commands["implement"] = str(script)
        routes(self.project).publish()
        self.addCleanup(routes(self.project).close)
        thread, timing = self.when_ready(self.steer_now_by_cli)

        output, timed_out = self.timed()
        thread.join()

        code, said = timing["said"]
        self.assertEqual(code, 0, said)
        self.assertIn("cannot resume the implementer session"
                      " (fallback implementer route)", said)
        self.assertEqual((str(output), timed_out), ("finished", False))
        self.assertEqual(self.launches(), 1)
        self.assertEqual(self.events("steer_interrupt"), [])
        self.assertEqual(self.notes(), [(None, None)])

    def test_a_reviewer_turn_runs_on_and_the_note_waits_for_the_implementer(self):
        reviewer = self.root / "reviewer"
        reviewer.write_text(
            "#!/bin/sh\n"
            f'STATE="{self.state}"\n'
            'echo $$ > "$STATE/pid"\ntouch "$STATE/ready"\n'
            f'sleep {4 * BEAT}\necho reviewed\n')
        reviewer.chmod(0o755)
        self.claude(first="echo '{\"result\": \"applied\"}'\n")
        config = (self.db.parent / "config.toml").read_text()
        self.configure(config.replace(
            "[agents.implementer]", f'[agents]\nreviewer = "{reviewer}"\n'
            "[agents.implementer]"))
        store.record_agent_session(self.conn, self.run_id, "a-session",
                                   "implement", "primary")
        (self.target / "b.txt").write_text("candidate\n")
        self.git("add", "b.txt")
        self.git("commit", "-q", "-m", "candidate")
        candidate = self.git("rev-parse", "HEAD")
        thread, _ = self.when_ready(self.interrupt_in_store)

        output = agent(self.project, "review", "Review it.", self.target,
                       base_sha=self.base, candidate_sha=candidate,
                       conn=self.conn, run_id=self.run_id)
        thread.join()

        self.assertEqual((str(output), output.exit_code), ("reviewed", 0))
        self.assertEqual(self.events("steer_interrupt"), [])
        self.assertEqual(self.notes(), [(1, None)])
        steer_turn(self.project, self.conn, self.run_id, BEAT, self.target, 25,
                   "the ticket", candidate, timed=implement._timed,
                   check_cap=lambda *args: None)
        self.assertIn(NOTE, self.argv(1)[-1])
        self.assertEqual(self.notes(), [(1, self.run_id)])

    def test_a_ready_ticket_with_no_run_refuses_now_and_writes_nothing(self):
        self.claude()
        store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id="issue-9",
            linear_identifier="KO-9", title="ticket 9",
            acceptance_criteria=["Given it, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * 60000)
        self.conn.commit()
        before = list(self.conn.iterdump())

        code, said = self.holo("steer", "KO-9", "--now", "-n", NOTE)

        self.assertNotEqual(code, 0, said)
        self.assertIn("KO-9 is ready", said)
        self.assertEqual(list(self.conn.iterdump()), before)
