"""A container review records the reviewer's Codex session and keeps its transcript,
and a refused adversary exit comes back as the adversary's reply.

Run: python3 -m unittest discover -s tests -p 'test_container_review_session.py' -v
"""
from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import holophyte.config.project  # noqa: E402 - after the sys.path insert above
import review_runner  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.agents import roles, transcripts  # noqa: E402
from holophyte.loop.gates import InfraFailure  # noqa: E402
from holophyte.review import adversary  # noqa: E402

# `run` stands in for the container: it runs the built script's Codex statement
# with the positional arguments `docker run` was handed, the mounts' host sides
# standing in for /opt/codex/bin and the reviewer home. Each run is logged.
DOCKER = """#!{python}
import os, sys
args = sys.argv[1:]
if args[0] == "inspect":
    sys.exit(1)
if args[0] != "run":
    sys.exit(0)
with open(os.environ["STUB_LAUNCHES"], "a") as launches:
    launches.write("run\\n")
host = {{v.split(":")[1]: v.split(":")[0]
        for a, v in zip(args, args[1:]) if a == "--volume"}}
shell = args.index("/bin/sh")
script, positional = args[shell + 3], args[shell + 4:]
statement = script[script.index("exec /opt/codex/bin/codex"):]
statement = statement.replace("/opt/codex/bin", host["/opt/codex/bin"])
os.environ["HOME"] = host["/home/reviewer"]
print("PREFLIGHT_OK candidate=shim", file=sys.stderr, flush=True)
os.execv("/bin/sh", ["/bin/sh", "-eu", "-c", statement, *positional])
"""

# Writes a rollout under its home when told a name, a newer forged file when
# told a decoy name, and opens the stream with `thread.started` when told an id.
# Told a provider error, it ends the turn on that error, after the message it is
# told if any, and exits 1.
CODEX = """#!{python}
import json, os, pathlib, sys
thread = os.environ.get("STUB_THREAD_ID")
rollout = os.environ.get("STUB_ROLLOUT")
decoy = os.environ.get("STUB_DECOY")
day = pathlib.Path(os.environ["HOME"], ".codex", "sessions", "2026", "10", "05")
if rollout:
    day.mkdir(parents=True, exist_ok=True)
    (day / rollout).write_text(json.dumps({{"type": "session_meta"}}) + "\\n")
if decoy:
    day.mkdir(parents=True, exist_ok=True)
    (day / decoy).write_text("forged\\n")
    os.utime(day / decoy, (4e9, 4e9))
events = [{{"type": "thread.started", "thread_id": thread}}] if thread else []
failure = os.environ.get("STUB_PROVIDER_ERROR")
message = os.environ.get("STUB_MESSAGE", "" if failure else "VERDICT: APPROVE")
items = [{{"type": "command_execution", "exit_code": 0}}]
if message:
    items.append({{"type": "agent_message", "text": message}})
events += [{{"type": "item.completed", "item": item}} for item in items]
if failure:
    events += [{{"type": "error", "message": failure}},
               {{"type": "turn.failed", "error": {{"message": failure}}}}]
for event in events:
    print(json.dumps(event))
sys.exit(1 if failure else 0)
"""

THREAD = "0199b2c4-7e1a-7c30-9a51-3f0d2e6b8a14"
ROLLOUT = f"rollout-2026-10-05T09-00-00-{THREAD}.jsonl"
REFUSAL = ("This content was flagged for possible cybersecurity risk. If this"
           " seems wrong, try rephrasing your request.")


class ContainerReviewSessionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        repo = self.root / "repo"
        repo.mkdir()
        git = ["git", "-c", "user.name=Test", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git, "init", "-q", "-b", "main"], cwd=repo, check=True)
        (repo / "value.txt").write_text("candidate\n")
        subprocess.run([*git, "add", "value.txt"], cwd=repo, check=True)
        subprocess.run([*git, "commit", "-qm", "candidate"], cwd=repo, check=True)
        self.sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, body in (("docker", DOCKER), ("codex", CODEX),
                           ("codex-code-mode-host", "#!/bin/sh\nexit 0\n")):
            (bin_dir / name).write_text(body.format(python=sys.executable))
            (bin_dir / name).chmod(0o755)
        (self.root / "auth.json").write_text("{}")
        env = patch.dict(os.environ, {
            "HOLOPHYTE_HOME": str(self.root / "home"),
            "STUB_LAUNCHES": str(self.root / "launches"),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"})
        env.start()
        self.addCleanup(env.stop)
        self.scratch = self.root / "reviews"
        for name, value in (("SCRATCH_ROOT", self.scratch),
                            ("CODEX_AUTH", self.root / "auth.json")):
            patcher = patch.object(review_runner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.project = holophyte.config.project.Project.locate(repo)
        self.project.holo_dir.mkdir(parents=True)
        self.conn = store.open(str(self.project.store_path))
        self.addCleanup(self.conn.close)
        project_id = store.tickets.ensure_project(self.conn, "team", repo)
        ticket = store.tickets.mirror_ticket(
            self.conn, project_id, linear_issue_id="i", linear_identifier="KO-1",
            title="t", acceptance_criteria=["c"], verification_commands=["true"])
        store.tickets.transition(self.conn, ticket, "in_flight")
        self.run_id = store.claim(self.conn, project_id, ticket)
        self.kept = self.project.holo_dir / "transcripts"

    def review(self, thread, rollout):
        stub = {"STUB_THREAD_ID": thread or "", "STUB_ROLLOUT": rollout or ""}
        with patch.dict(os.environ, stub):
            output = roles.agent(
                self.project, "review", "judge the candidate", self.project.path,
                base_sha=self.sha, candidate_sha=self.sha, conn=self.conn,
                run_id=self.run_id, review_round=2)
        self.assertEqual(output, "VERDICT: APPROVE")
        self.assertEqual(list(self.scratch.iterdir()), [])
        return [json.loads(payload) for (payload,) in self.conn.execute(
            "SELECT payload FROM runEvents WHERE runId=? AND kind='agent_session'",
            (self.run_id,))]

    def test_the_turn_records_the_codex_thread_and_keeps_its_rollout(self):
        sessions = self.review(THREAD, ROLLOUT)

        self.assertEqual(sessions, [{"session_id": THREAD, "role": "review",
                                     "route": "primary", "round": 2}])
        kept = self.kept / ROLLOUT
        self.assertEqual(json.loads(kept.read_text()), {"type": "session_meta"})
        self.assertEqual(stat.S_IMODE(kept.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.kept.stat().st_mode), 0o700)
        self.assertEqual(transcripts.locate("codex", THREAD, self.kept),
                         kept.resolve())

    def test_a_newer_file_named_for_another_session_does_not_replace_its_transcript(
            self):
        earlier = self.kept / "rollout-2026-10-04T09-00-00-earlier-thread.jsonl"
        self.kept.mkdir(mode=0o700)
        earlier.write_text("earlier\n")

        with patch.dict(os.environ, {"STUB_DECOY": earlier.name}):
            sessions = self.review(THREAD, ROLLOUT)

        self.assertEqual([s["session_id"] for s in sessions], [THREAD])
        self.assertEqual(earlier.read_text(), "earlier\n")
        self.assertEqual(json.loads((self.kept / ROLLOUT).read_text()),
                         {"type": "session_meta"})

    def test_a_copy_failing_midway_leaves_no_partial_rollout_to_block_the_next(self):
        keep = review_runner.keep_transcript

        def keep_on_a_full_disk(*args):
            def fill(reader, writer):
                writer.write(reader.read(4))
                writer.flush()
                raise OSError(errno.ENOSPC, "No space left on device")
            with patch.object(review_runner.shutil, "copyfileobj", fill):
                return keep(*args)

        with patch.object(review_runner, "keep_transcript", keep_on_a_full_disk):
            self.assertEqual(self.review(THREAD, ROLLOUT), [])
        self.assertEqual(list(self.kept.iterdir()), [])

        self.assertEqual([s["session_id"] for s in self.review(THREAD, ROLLOUT)],
                         [THREAD])
        self.assertEqual(json.loads((self.kept / ROLLOUT).read_text()),
                         {"type": "session_meta"})

    def test_a_missing_or_malformed_id_or_rollout_records_and_keeps_nothing(self):
        for label, thread, rollout in (
                ("no id", None, ROLLOUT),
                ("malformed id", "not an id",
                 "rollout-2026-10-05T09-00-00-not an id.jsonl"),
                ("no rollout", THREAD, None)):
            with self.subTest(label):
                self.assertEqual(self.review(thread, rollout), [])
                self.assertFalse(self.kept.exists())

    def turn(self, role, stub):
        with patch.dict(os.environ, stub):
            return roles.agent(
                self.project, role, "attack the candidate",
                self.project.path, base_sha=self.sha, candidate_sha=self.sha,
                conn=self.conn, run_id=self.run_id, review_round=1)

    def events(self, kind):
        return [json.loads(payload) for (payload,) in self.conn.execute(
            "SELECT payload FROM runEvents WHERE runId=? AND kind=?",
            (self.run_id, kind))]

    def test_a_refused_adversary_exit_is_returned_as_its_reply(self):
        with patch.dict(os.environ, {"STUB_PROVIDER_ERROR": REFUSAL}):
            output = roles.agent(
                self.project, "adversary", "attack the candidate",
                self.project.path, base_sha=self.sha, candidate_sha=self.sha,
                conn=self.conn, run_id=self.run_id, review_round=1)

        self.assertTrue(adversary.refused(output))
        turns = [json.loads(payload) for (payload,) in self.conn.execute(
            "SELECT payload FROM runEvents WHERE runId=? AND kind='agent_turn'",
            (self.run_id,))]
        self.assertEqual([t["exit_status"] for t in turns], [1])

    def test_a_refused_adversary_exit_settles_as_refused_with_no_fallback(self):
        self.project.config_path.write_text(
            '[agents]\nreview_fallback_model = "gpt-5.6-sol"\n'
            'review_fallback_effort = "high"\n')
        self.project = holophyte.config.project.Project.locate(self.project.path)
        plan = adversary.Pass(1, "high", "full", "candidate", self.sha, self.sha,
                              adversary.Family("codex", "gpt-6.1-sol", "high"))

        with patch.dict(os.environ, {"STUB_PROVIDER_ERROR": REFUSAL}):
            attacked = adversary.attack(
                self.project, self.conn, self.run_id, self.project.path,
                self.sha, "the ticket", plan, roles.agent)
        adversary.settle(self.project, self.conn, self.run_id, None, "KO-1",
                         plan, attacked)

        self.assertEqual((self.root / "launches").read_text(), "run\n")
        [event] = self.events("adversary_round")
        self.assertEqual((event["outcome"], event["findings"],
                          event["concerns"]), ("refused", [], []))
        self.assertEqual(self.events("route_fallback"), [])

    def test_an_exit_whose_only_refusal_line_is_model_text_fails_the_route(self):
        stub = {"STUB_MESSAGE": f"Matching '{REFUSAL}' is unsafe.",
                "STUB_PROVIDER_ERROR": "stream disconnected before completion"}
        with self.assertRaises(InfraFailure) as raised:
            self.turn("adversary", stub)
        self.assertEqual(raised.exception.failure_kind, "review_route")

    def test_a_refused_exit_of_the_primary_reviewer_fails_the_route(self):
        with self.assertRaises(InfraFailure) as raised:
            self.turn("review", {"STUB_PROVIDER_ERROR": REFUSAL})
        self.assertEqual(raised.exception.failure_kind, "review_route")


if __name__ == "__main__":
    unittest.main()
