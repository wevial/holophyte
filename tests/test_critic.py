"""The critic seat's startup probe: absent without `[agents.critic]`, run in
a throwaway detached checkout of `main` when configured, and never able to
stop the loop. And its question at the claim (KO-715): a ticket filed long
ago, or naming a file main changed since, is put to the critic before it is
claimed; FRESH claims, STALE and UNSURE park, and a critic that fails or
answers no verdict claims anyway with a warning on the run.

Run: python3 -m unittest tests.test_critic -v
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
# Both discovery and named-module unittest commands need the harness on sys.path.
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit, Critic, FakeAgent  # noqa: E402
from loop_fixture import VALID_BODY, LoopFixture, StubProvider, a_task  # noqa: E402

import holophyte.agents  # noqa: E402 - after the sys.path insert above
import holophyte.claim  # noqa: E402 - after the sys.path insert above
import holophyte.harness  # noqa: E402 - after the sys.path insert above
import holophyte.loop  # noqa: E402 - after the sys.path insert above
import holophyte.pool  # noqa: E402 - after the sys.path insert above
import holophyte.project  # noqa: E402 - after the sys.path insert above
import holophyte.runs  # noqa: E402 - after the sys.path insert above
import store.tickets as tickets  # noqa: E402 - after the sys.path insert above
from holophyte.agent_routes import reset, routes  # noqa: E402

# The fake codex: records its cwd, the HEAD there and whether HEAD is
# detached, then answers the probe, or exits 1 when told to.
FAKE_CODEX = """
import json, os, subprocess, sys
git = lambda *args: subprocess.run(["git", *args], capture_output=True, text=True)
with open(os.environ["FAKE_CRITIC_CALLS"], "a") as calls:
    calls.write(json.dumps({
        "cwd": os.getcwd(), "head": git("rev-parse", "HEAD").stdout.strip(),
        "detached": git("symbolic-ref", "-q", "HEAD").returncode != 0}) + "\\n")
if os.environ.get("FAKE_CRITIC_FAIL"):
    print("error: the critic is down")
    sys.exit(1)
print("ready")
"""


class CriticProbeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("commit", "--allow-empty", "-qm", "base")
        self.main = self.git("rev-parse", "main")
        self.holo = root / "holo"
        self.holo.mkdir()
        self.fake = root / "codex"
        self.fake.write_text(f"#!{sys.executable}\n{FAKE_CODEX}")
        self.fake.chmod(0o755)
        self.calls = root / "calls.jsonl"
        env = patch.dict("os.environ", {"FAKE_CRITIC_CALLS": str(self.calls)})
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             *args], cwd=self.repo, text=True).strip()

    def target(self, config):
        (self.holo / "config.toml").write_text(config)
        project = holophyte.project.Project(
            path=self.repo, holo_dir=self.holo, store_path=self.holo / "store.db",
            config_path=self.holo / "config.toml",
            worktrees=self.repo.parent / "repo.worktrees")
        self.addCleanup(reset, project)
        return project

    def start(self, project, **options):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            started = holophyte.agents.startup_routes(
                project, SimpleNamespace(team="test"), **options)
        return started, printed.getvalue()

    def critic_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def critic(self):
        return self.target(f'[agents.critic]\n[harnesses]\ncodex = "{self.fake}"\n')

    def test_no_critic_table_means_no_seat_and_no_probe(self):
        project = self.target("")
        self.assertIsNone(holophyte.harness.critic_seat(project))
        with patch.object(holophyte.agents, "critic_workspace") as workspace:
            started, printed = self.start(project)
        self.assertTrue(started)
        workspace.assert_not_called()
        self.assertFalse(self.calls.exists())
        self.assertNotIn("critic", printed)

    def test_probe_runs_in_a_detached_checkout_of_main_then_removes_it(self):
        started, printed = self.start(self.critic())
        self.assertTrue(started)
        self.assertIn("[holo2] critic probe passed", printed)
        [call] = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(call["head"], self.main)
        self.assertTrue(call["detached"])
        self.assertNotEqual(Path(call["cwd"]).resolve(), self.repo.resolve())
        self.assertFalse(Path(call["cwd"]).exists())
        self.assertNotIn(call["cwd"], self.git("worktree", "list"))

    def test_a_failed_probe_turns_the_critic_off_but_starts_the_loop(self):
        project = self.critic()
        with patch.dict("os.environ", {"FAKE_CRITIC_FAIL": "1"}):
            started, printed = self.start(project)
        self.assertTrue(started)
        self.assertIn("critic probe failed (exit 1)", printed)
        self.assertIn(
            "[holo2] critic route down; claims skip the relevance check", printed)
        self.assertTrue(routes(project).critic_failed)
        [call] = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertFalse(Path(call["cwd"]).exists())

    def test_a_scheduler_hands_its_failed_probe_to_the_workers_it_spawns(self):
        project = self.critic()
        with patch.dict("os.environ", {"FAKE_CRITIC_FAIL": "1"}):
            started, _ = self.start(project, activate=False)
        self.assertTrue(started)
        spawned = []

        def spawn(argv, env, **_):
            spawned.append(env)
            return SimpleNamespace(pid=1)
        with patch.object(holophyte.pool, "SPAWN", spawn), \
                contextlib.redirect_stdout(io.StringIO()):
            holophyte.pool._spawn_worker(project, 1)
        self.assertEqual(spawned[0].get(holophyte.pool.CRITIC_DOWN_ENV), "1")

    def test_a_worker_with_a_writer_keeps_the_critic_off_without_a_probe(self):
        writer = self.repo.parent / "writer.sh"
        writer.write_text("#!/bin/sh\necho ready\n")
        writer.chmod(0o755)
        project = self.target(f'[agents]\nwriter = "{writer}"\n[agents.critic]\n'
                              f'[harnesses]\ncodex = "{self.fake}"\n')
        seen = []

        def claim(target, _):
            seen.append(routes(target).critic_failed)
        with patch.dict("os.environ", {holophyte.pool.CRITIC_DOWN_ENV: "1"}), \
                patch.object(holophyte.pool, "_worker", claim), \
                contextlib.redirect_stdout(io.StringIO()):
            holophyte.pool.worker(project, SimpleNamespace(team="test"))
        self.assertEqual(seen, [True])
        self.assertEqual(self.critic_calls(), [])


HOUR_MS = 3600 * 1000
# A startup probe that passes: the loop tests' critic seat is this script,
# not `codex`, and every turn after the probe is the fake agent's.
PROBE = f"#!{sys.executable}\nprint('ready')\n"


def naming(path):
    """The fixture's valid body, its implementation notes naming `path`."""
    return VALID_BODY.replace(
        "## Implementation notes\n\n* None.\n",
        f"## Implementation notes\n\n* Edit `{path}` where the thing lives.\n")


def filed(hours_ago, n, body=VALID_BODY, **fields):
    """Ticket `n`, filed `hours_ago` hours ago, carrying `body`."""
    return dict(a_task(n), body=body,
                filed_at=int(time.time() * 1000) - int(hours_ago * HOUR_MS),
                **fields)


class CriticClaimTests(LoopFixture):
    def setUp(self):
        super().setUp()
        probe = self.target.parent / "critic-probe"
        probe.write_text(PROBE)
        probe.chmod(0o755)
        self.configure(f'[agents.critic]\n[harnesses]\ncodex = "{probe}"\n'
                       '[loop]\ncritic_after_hours = 12\n')
        self.addCleanup(reset, self.project)

    def commit(self, path, text, hours_ago=0):
        """Commit `path` on main, dated `hours_ago` hours back."""
        (self.target / path).parent.mkdir(parents=True, exist_ok=True)
        (self.target / path).write_text(text)
        when = f"@{int(time.time() - hours_ago * 3600)} +0000"
        self.git("add", "-A")
        with patch.dict(os.environ, {"GIT_AUTHOR_DATE": when,
                                     "GIT_COMMITTER_DATE": when}):
            self.git("commit", "-q", "-m", f"touch {path}")

    def critic_goals(self, fake):
        return [turn.goal for turn in fake.turns if turn.role == "critic"]

    def warnings_by_ticket(self):
        return self.read(
            "SELECT t.linearIdentifier, e.summary FROM runEvents e"
            " JOIN runs r ON r.id = e.runId JOIN tickets t ON t.id = r.ticketId"
            " WHERE e.kind = 'warning' AND e.summary LIKE 'critic:%'"
            " ORDER BY r.id")

    def test_the_critic_is_asked_about_old_or_touched_tickets_only(self):
        self.commit("holophyte/old.py", "x = 1\n", hours_ago=2)
        self.commit("holophyte/route.py", "route = 1\n", hours_ago=2)
        self.commit("holophyte/route.py", "route = 2\n")
        tasks = [filed(1, 1, naming("holophyte/old.py")),
                 filed(13, 2, naming("holophyte/old.py")),
                 filed(1, 3, naming("holophyte/route.py"))]
        provider = StubProvider(*tasks)
        conn = holophyte.runs.open_store(self.project)
        self.addCleanup(conn.close)
        project_id = tickets.ensure_project(conn, provider.team, self.target)
        fake = FakeAgent(Critic(), Critic())

        with patch.object(holophyte.loop, "agent", fake), \
                patch.object(sys, "stdout", io.StringIO()):
            admitted = [holophyte.claim._admit_ticket(
                self.project, conn, project_id, provider, task,
                SimpleNamespace(trips=[], watched=[])) for task in tasks]

        self.assertNotIn(None, admitted)
        goals = self.critic_goals(fake)
        self.assertEqual(len(goals), 2)
        self.assertIn("Ticket KO-132:", goals[0])
        self.assertIn("Ticket KO-133:", goals[1])

    def test_a_fresh_answer_claims_and_the_brief_names_what_merged_since(self):
        provider = StubProvider(dict(a_task(1), title="add the first thing"),
                                filed(13, 2))

        fake, _ = self.loop(Commit("first thing", path="first.txt"), APPROVE,
                            Critic(), Commit("second thing"), APPROVE,
                            provider=provider)

        self.assertEqual(
            self.read("SELECT t.linearIdentifier FROM runs r JOIN tickets t"
                      " ON t.id = r.ticketId ORDER BY r.id"),
            [("KO-131",), ("KO-132",)])
        [goal] = self.critic_goals(fake)
        self.assertIn(VALID_BODY.strip(), goal)
        merged = next(line for line in goal.splitlines()
                      if line.startswith("- KO-131"))
        self.assertIn("add the first thing", merged)
        self.assertIn("first.txt", merged)

    def test_stale_and_unsure_answers_park_the_ticket_with_the_reason(self):
        provider = StubProvider(filed(13, 1), filed(13, 2))

        out = self.main_output(
            Critic("Looked at main.\nFRESHNESS: STALE already done by KO-5"),
            Critic("FRESHNESS: UNSURE cannot tell whether the route moved"),
            provider=provider)

        self.assertEqual(self.read("SELECT COUNT(*) FROM runs"), [(0,)])
        self.assertEqual(
            self.read("SELECT linearIdentifier, status FROM tickets"
                      " ORDER BY linearIdentifier"),
            [("KO-131", "needs_spec"), ("KO-132", "needs_spec")])
        comments = dict(provider.comments)
        self.assertIn("critic: stale", comments["iss-131"])
        self.assertIn("already done by KO-5", comments["iss-131"])
        self.assertIn("critic: unsure", comments["iss-132"])
        self.assertIn("cannot tell whether the route moved", comments["iss-132"])
        self.assertIn("KO-131 skipped", out)

    def test_a_critic_that_fails_or_answers_no_verdict_claims_with_a_warning(self):
        provider = StubProvider(filed(13, 1), filed(13, 2), filed(13, 3))

        self.loop(Critic("I read the code and have views."),
                  Commit("one"), APPROVE,
                  Critic("Hard to say.\nFRESHNESS: MAYBE"),
                  Commit("two"), APPROVE,
                  Critic(raises=RuntimeError("the critic crashed")),
                  Commit("three"), APPROVE, provider=provider)

        warnings = self.warnings_by_ticket()
        self.assertEqual([ticket for ticket, _ in warnings],
                         ["KO-131", "KO-132", "KO-133"])
        self.assertIn("no FRESHNESS verdict", warnings[0][1])
        self.assertIn("no FRESHNESS verdict", warnings[1][1])
        self.assertIn("the critic crashed", warnings[2][1])


if __name__ == "__main__":
    unittest.main()
