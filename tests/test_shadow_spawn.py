"""The shadow implementer started beside a fresh claim's primary: the loop
spawns it detached and never waits on it, and `factory.py --shadow` runs one
shadow a project under the project's shadow lock.

Run: python3 -m unittest discover -s tests -p 'test_shadow_spawn.py' -v
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.board.projection
import holophyte.config.project
import holophyte.loop.claim
import holophyte.loop.pipeline
import holophyte.loop.shadow_spawn
import store
import store.tickets
from holophyte.agents.probes import PROBE_GOAL
from holophyte.loop.runs import open_store
from holophyte.loop.shadow import ShadowBrief
from holophyte.loop.shadow_spawn import write_brief
from holophyte.loop.stop import command
from tests.fake_agent import APPROVE, REQUEST_CHANGES, Commit
from tests.loop_fixture import BRANCH, LoopFixture, StubProvider, a_task

ROOT = Path(__file__).resolve().parents[1]
SHADOW = '[agents.implementer_shadow]\nharness = "claude"\nmodel = "sonnet"\n'
T0 = 1_700_000_000_000


class NeverExits:
    pid = 4242

    def wait(self, timeout=None):
        raise AssertionError("the run waited on its shadow")

    communicate = wait


class Spawn:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error is not None:
            raise self.error
        return NeverExits()


class ShadowStartTests(LoopFixture):
    def shadow_loop(self, *script, config=SHADOW, spawn=None, task=None):
        self.configure(config)
        spawn = spawn or Spawn()
        with patch.object(holophyte.loop.shadow_spawn, "SPAWN", spawn):
            fake, _ = self.loop(*script, provider=StubProvider(task or a_task()))
        return spawn, fake

    def last_outcome(self):
        return self.read("SELECT outcome FROM runs ORDER BY id DESC LIMIT 1")[0][0]

    def rounds(self, task_id):
        return self.read(
            "SELECT r.round, r.verdict FROM reviewRounds r JOIN runs u"
            " ON u.id = r.runId JOIN tickets t ON t.id = u.ticketId"
            f" WHERE t.linearIdentifier = '{task_id}' ORDER BY r.round")

    def test_a_fresh_claim_spawns_the_shadow_with_the_primarys_brief(self):
        spawn, fake = self.shadow_loop(Commit("the thing"), APPROVE)
        self.assertEqual(self.last_outcome(), "merged")
        [(argv, kwargs)] = spawn.calls
        self.assertEqual(argv[-3::2], ["--shadow", str(self.target)])
        self.assertEqual((kwargs["start_new_session"], kwargs["stdin"]),
                         (True, subprocess.DEVNULL))
        brief = Path(argv[-2])
        self.assertEqual(stat.S_IMODE(brief.stat().st_mode), 0o600)
        fields = json.loads(brief.read_text())
        self.assertEqual(fake.roles[0], "implement")
        self.assertEqual(fields["goal"], fake.turns[0].goal)
        self.assertEqual(fields["base_sha"], self.base)
        self.assertEqual(fields["branch"], BRANCH)
        [(payload,)] = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'shadow_started'")
        self.assertEqual(json.loads(payload), {
            "pid": NeverExits.pid, "branch": "shadow/ko-131-add-a-thing",
            "route": "claude sonnet", "error": None})

    def test_a_shadow_that_never_exits_leaves_the_runs_review_rounds_alone(self):
        def script(n):
            return (Commit("the thing", path=f"thing-{n}.txt"), REQUEST_CHANGES,
                    Commit("the fix", path=f"fix-{n}.txt"), APPROVE)
        self.shadow_loop(*script(1), config="", task=a_task(1))
        spawn, _ = self.shadow_loop(*script(2), task=a_task(2))
        self.assertEqual(len(spawn.calls), 1)
        self.assertEqual(self.last_outcome(), "merged")
        self.assertEqual(len(self.rounds("KO-131")), 2)
        self.assertEqual(self.rounds("KO-132"), self.rounds("KO-131"))

    def test_a_brief_that_cannot_be_written_is_recorded_and_the_run_merges(self):
        shadows = self.db.parent / "shadows"
        shadows.mkdir()
        shadows.chmod(0)
        self.addCleanup(shadows.chmod, 0o700)
        spawn, _ = self.shadow_loop(Commit("the thing"), APPROVE)
        self.assertEqual(self.last_outcome(), "merged")
        self.assertEqual(spawn.calls, [])
        [(payload,)] = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'shadow_started'")
        self.assertIn("Permission denied", json.loads(payload)["error"])

    def test_a_spawn_that_raises_is_recorded_and_the_run_still_merges(self):
        self.shadow_loop(Commit("the thing"), APPROVE,
                         spawn=Spawn(OSError("spawn refused")))
        self.assertEqual(self.last_outcome(), "merged")
        [(payload,)] = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'shadow_started'")
        started = json.loads(payload)
        self.assertIsNone(started["pid"])
        self.assertIn("spawn refused", started["error"])
        self.assertEqual(list(self.db.parent.glob("shadows/*.json")), [])

    def test_no_shadow_without_the_key(self):
        spawn, _ = self.shadow_loop(Commit("the thing"), APPROVE, config="")
        self.assertEqual(self.last_outcome(), "merged")
        self.assertEqual(spawn.calls, [])

    def test_no_shadow_for_a_claim_that_reuses_a_clean_leftover_worktree(self):
        wt = self.worktrees / "ko-131-add-a-thing"
        self.git("worktree", "add", "--detach", str(wt), "main")
        self.git("checkout", "-q", "-b", BRANCH, cwd=wt)
        spawn, fake = self.shadow_loop(Commit("the thing"), APPROVE)
        self.assertEqual(fake.roles[0], "implement")
        self.assertEqual(self.last_outcome(), "merged")
        self.assertEqual(spawn.calls, [])

    def test_no_shadow_for_a_run_resumed_from_a_pause_before_its_worktree(self):
        cut = holophyte.loop.claim._cut_worktree

        def pause_first(project, conn, run_id, *args):
            store.pause(conn, run_id, "reboot writer")
            return cut(project, conn, run_id, *args)

        with patch.object(holophyte.loop.pipeline, "_cut_worktree", pause_first):
            self.shadow_loop()
        self.assertEqual(self.last_outcome(), "paused")
        command(self.project, "KO-131", None, resume=True)
        spawn, fake = self.shadow_loop(Commit("the thing"), APPROVE)
        self.assertEqual(fake.roles[0], "implement")
        self.assertEqual(self.last_outcome(), "merged")
        self.assertEqual(spawn.calls, [])

    def test_no_shadow_for_a_requeued_run_opened_by_its_note(self):
        conn = open_store(self.project)
        self.addCleanup(conn.close)
        project_id = store.tickets.ensure_project(conn, StubProvider.TEAM,
                                                  self.target)
        ticket = holophyte.board.projection.mirror_task(conn, project_id, a_task())
        run = store.claim(conn, project_id, ticket, now=T0)
        store.tickets.transition(conn, ticket, "in_flight")
        store.release(conn, run, "failed", "review rejected", now=T0 + 1)
        store.requeue(conn, ticket, "fix the parser crash", now=T0 + 2)
        conn.commit()
        spawn, fake = self.shadow_loop(Commit("the thing"), APPROVE)
        self.assertIn("fix the parser crash", fake.turns[0].goal)
        self.assertEqual(self.last_outcome(), "merged")
        self.assertEqual(spawn.calls, [])


IDENTITY = ["-c", "user.name=Shadow", "-c", "user.email=shadow@example.invalid"]
IMPLEMENTER = f"""#!{sys.executable}
import json, subprocess, sys
if sys.argv[-1] == {PROBE_GOAL!r}:
    print(json.dumps({{"type": "result", "result": "ready"}}))
    sys.exit(0)
open("done.txt", "w").write("ok\\n")
subprocess.run(["git", *{IDENTITY!r}, "add", "done.txt"], check=True)
subprocess.run(["git", *{IDENTITY!r}, "commit", "-qm", "done"], check=True)
print(json.dumps({{"type": "result", "result": "done"}}))
"""
HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, b"7 0.0\\n")
print("held", flush=True)
sys.stdin.read()
"""
PUBLISHING_HOLDER = """
import fcntl, os, sys
arbiter = os.open(sys.argv[1] + ".arbiter", os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(arbiter, fcntl.LOCK_EX)
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.readline()
os.ftruncate(fd, 0)
os.write(fd, b"8 0.0\\n")
os.close(arbiter)
print("published", flush=True)
sys.stdin.read()
"""


class ShadowModeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git(*IDENTITY, "commit", "--allow-empty", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.project = holophyte.config.project.Project.locate(self.repo)
        self.project.holo_dir.mkdir(parents=True)
        binary = root / "claude-shadow"
        binary.write_text(IMPLEMENTER)
        binary.chmod(0o755)
        self.project.config_path.write_text(
            f'{SHADOW}effort = "high"\n[harnesses]\nclaude = "{binary}"\n')
        conn = store.open(self.project.store_path)
        store.init(conn)
        project = store.ensure_project(conn, "test", self.repo)
        ticket = store.mirror_ticket(conn, project, "KO-7", "KO-7", "shadow",
                                     acceptance_criteria=["done"],
                                     verification_commands=["true"])
        self.run_id = store.claim(conn, project, ticket)
        conn.close()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def shadow(self):
        brief, done = self.start_shadow()
        out, err = done.communicate(timeout=120)
        self.assertEqual(done.returncode, 0, out + err)
        self.assertFalse(brief.exists())

    def start_shadow(self):
        brief = write_brief(self.project, self.run_id, ShadowBrief(
            goal="Create done.txt saying ok", ticket="Create done.txt",
            criteria=[], task_id="KO-7", verify="grep -qx ok done.txt",
            contracts=None, base_sha=self.base, branch="task/ko-7-thing",
            seconds=60))
        return brief, subprocess.Popen(
            [sys.executable, str(ROOT / "factory.py"), "--shadow", str(brief),
             str(self.repo)], cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)

    def events(self, kind):
        conn = store.open(self.project.store_path)
        self.addCleanup(conn.close)
        return [json.loads(payload) for (payload,) in conn.execute(
            "SELECT payload FROM runEvents WHERE runId = ? AND kind = ?",
            (self.run_id, kind))]

    def test_the_mode_runs_the_brief_as_a_shadow_and_records_its_result(self):
        self.shadow()
        [result] = self.events("shadow_result")
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(result["base_sha"], self.base)
        self.assertEqual(self.git("show", "shadow/ko-7-thing:done.txt"), "ok")

    def test_a_held_shadow_lock_skips_naming_the_busy_run_until_it_exits(self):
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER,
             str(self.project.holo_dir / "shadow.lock")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline(), "held\n")
        self.shadow()
        self.assertEqual(self.events("shadow_skipped"), [{"busy_run": 7}])
        self.assertEqual(self.events("shadow_result"), [])
        self.assertEqual(self.git("branch", "--list", "shadow/*"), "")
        self.assertNotIn(".shadow", self.git("worktree", "list"))
        holder.stdin.close()
        holder.wait(timeout=30)
        holder.stdout.close()
        self.shadow()
        self.assertEqual(len(self.events("shadow_result")), 1)

    def test_a_skip_names_the_holder_that_has_published_not_a_stale_one(self):
        lock = self.project.holo_dir / "shadow.lock"
        lock.write_text("7 0.0\n")
        holder = subprocess.Popen(
            [sys.executable, "-c", PUBLISHING_HOLDER, str(lock)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline(), "locked\n")
        _, contender = self.start_shadow()
        self.addCleanup(contender.kill)
        with contextlib.suppress(subprocess.TimeoutExpired):
            contender.wait(timeout=3)
        holder.stdin.write("\n")
        holder.stdin.flush()
        self.assertEqual(holder.stdout.readline(), "published\n")
        out, err = contender.communicate(timeout=120)
        self.assertEqual(contender.returncode, 0, out + err)
        self.assertEqual(self.events("shadow_skipped"), [{"busy_run": 8}])
        holder.stdin.close()
        holder.wait(timeout=30)
        holder.stdout.close()


if __name__ == "__main__":
    unittest.main()
