"""Execution-contract tests for Holophyte's named agent routes and the review
loop's control flow.

Run: python3 -m unittest discover -s tests -p 'test_factory_agents*' -v
"""
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
import holophyte.agents  # noqa: E402 - after the sys.path insert above
import holophyte.gates  # noqa: E402 - after the sys.path insert above
import holophyte.loop  # noqa: E402 - after the sys.path insert above
import holophyte.review  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above
import review_runner  # noqa: E402 - after the sys.path insert above
from tests.fake_agent import answer_scope  # noqa: E402 - after sys.path setup


def bare_target(case, path):
    """A `Target` at `path` whose state directory holds no config.

    The routes these tests pin are the defaults, so the config the value
    would read has to be absent -- in a directory of the test's own, not
    wherever `HOLOPHYTE_HOME` happens to point on this host. The directory
    is removed when `case` finishes.
    """
    path = Path(path)
    holo = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, holo, ignore_errors=True)
    return holophyte.target.Target(
        path=path, holo_dir=holo, store_path=holo / "store.db",
        config_path=holo / "config.toml",
        worktrees=path.parent / f"{path.name}.worktrees")


class AgentTurnEventTests(unittest.TestCase):
    def setUp(self):
        import store
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.target = bare_target(self, self.repo)
        for args in (("init", "-q"), ("-c", "user.name=Test", "-c",
                     "user.email=test@example.invalid", "commit", "--allow-empty",
                     "-qm", "base")):
            subprocess.run(["git", *args], cwd=self.repo, check=True)
        self.sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        self.conn = store.open(self.target.store_path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.ensure_project(self.conn, "test", self.repo)
        ticket = store.mirror_ticket(self.conn, project, "KO-573", "KO-573",
                                     "turn events", acceptance_criteria=["record"],
                                     verification_commands=["true"])
        self.run = store.claim(self.conn, project, ticket)

    def configure(self, **commands):
        self.target._config = None
        self.target.config_path.write_text("[agents]\n" + "".join(
            f"{key} = {json.dumps(value)}\n" for key, value in commands.items()))

    def stub(self, name, body):
        path = self.repo / name
        path.write_text(f"#!{sys.executable}\n" + body + "\n")
        path.chmod(0o755)
        return str(path)

    def turn(self, role="implement", **kwargs):
        return holophyte.agents.agent(
            self.target, role, "private prompt --model secret", self.repo,
            base_sha=self.sha, candidate_sha=self.sha,
            conn=self.conn, run_id=self.run, **kwargs)

    def events(self):
        return [json.loads(row[0]) for row in self.conn.execute(
            "SELECT payload FROM runEvents WHERE runId=? AND kind='agent_turn' "
            "ORDER BY seq", (self.run,))]

    def test_wrapper_session_recorded_before_scratch_cleanup(self):
        command = self.stub("review-session", "import os\nfrom pathlib import Path\n"
                            "p = Path(os.environ['HOLOPHYTE_REVIEW_SCRATCH'])\n"
                            "(p / 'session').write_text('opaque-session')\n"
                            "print(p)")
        self.configure(reviewer=command)
        output = self.turn('review', review_round=1)
        self.assertFalse(Path(str(output)).exists())
        events = [json.loads(row[0]) for row in self.conn.execute(
            "SELECT payload FROM runEvents WHERE kind='agent_session'")]
        self.assertEqual(len(events), 1)
        self.assertEqual({k: events[0][k] for k in ('session_id', 'role', 'route')},
                         dict(session_id='opaque-session', role='review',
                              route='primary'))
        self.assertIsNone(self.conn.execute(
            "SELECT providerSessionId FROM runs WHERE id=?", (self.run,)).fetchone()[0])

    def test_wrapper_rejects_invalid_sessions_and_accepts_length_boundary(self):
        for session in ('', 'two words', 'id\n', 'id\x00', 'x' * 201, 'x' * 200):
            with self.subTest(session_length=len(session)):
                command = self.stub('review-session',
                    "import os\nfrom pathlib import Path\n"
                    "p = Path(os.environ['HOLOPHYTE_REVIEW_SCRATCH'])\n"
                    f"(p / 'session').write_text({session!r})\nprint(p)")
                self.configure(reviewer=command)
                output = self.turn('review', review_round=1)
                self.assertFalse(Path(str(output)).exists())
                count = self.conn.execute(
                    "SELECT count(*) FROM runEvents WHERE kind='agent_session'"
                ).fetchone()[0]
                self.assertEqual(count, int(len(session) == 200))

    def test_roles_status_labels_and_probe_without_context(self):
        command = self.stub("devin-review", "import time; time.sleep(0.05)")
        self.configure(implementer=command + " -m first --model ignored",
                       reviewer=command, adjudicator=command,
                       writer=command + " --model writer-model")
        for role, label in (("implement", command + " first"),
                            ("review", command), ("adjudicate", command),
                            ("write", command + " writer-model")):
            self.turn(role)
            event = self.events()[-1]
            self.assertEqual({k: v for k, v in event.items() if k != "seconds"},
                             dict(role=role, label=label, route="primary",
                                  exit_status=0, timed_out=False))
            self.assertGreaterEqual(event["seconds"], 0.05)
        self.assertEqual(len(self.events()), 4)
        for context in ({}, {"conn": self.conn}, {"run_id": self.run}):
            holophyte.agents.agent(self.target, "implement", "probe", self.repo,
                                    **context)
        self.assertEqual(len(self.events()), 4)
        failing = self.stub("failed", "raise SystemExit(7)")
        self.configure(implementer=failing, reviewer=failing, writer=failing)
        for role in ("implement", "review", "write"):
            self.turn(role)
            self.assertEqual(self.events()[-1]["exit_status"], 7)
            self.assertFalse(self.events()[-1]["timed_out"])

    def test_timed_writer_delegation_preserves_role_and_implementer_identity(self):
        command = self.stub("implementer", "print('draft written')")
        writer = self.stub("writer", "raise SystemExit('writer must not run')")
        for refused in (False, True):
            with self.subTest(writer_refused=refused):
                self.configure(implementer=command + " -m implement-model",
                               **({"writer": writer} if refused else {}))
                holophyte.agents.routes(self.target).writer_failed = refused
                output, timed_out = holophyte.loop._timed(
                    self.target, self.conn, self.run, 60, self.repo, 1,
                    "write the PR", role="write")
                self.assertEqual(output, "draft written")
                self.assertFalse(timed_out)
                event = self.events()[-1]
                self.assertEqual(event["role"], "write")
                self.assertEqual(event["label"], command + " implement-model")
                self.assertEqual(event["route"], "primary")
                self.assertEqual(event["exit_status"], 0)
                self.assertFalse(event["timed_out"])
        self.assertEqual(len(self.events()), 2)

    def test_timeout_is_recorded_and_still_propagates(self):
        command = self.stub("slow", "import time; time.sleep(30)")
        self.configure(implementer=command)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.turn(timeout=0.05)
        event, = self.events()
        self.assertTrue(event["timed_out"])
        self.assertIsNone(event["exit_status"])
        self.assertGreaterEqual(event["seconds"], 0.05)

    def test_startup_fallback_attributes_only_the_launched_turn(self):
        from types import SimpleNamespace
        primary = self.stub("primary", "raise SystemExit(1)")
        fallback = self.stub("fallback", "print('ready')")
        self.configure(implementer=primary,
                       implementer_fallback=fallback + " --model fallback-model")
        self.addCleanup(holophyte.agents.routes(self.target).close)
        with patch("holophyte.operator._record_startup_probe"):
            self.assertTrue(holophyte.agents.startup_routes(
                self.target, SimpleNamespace(team="test")))
        self.assertEqual(self.events(), [])
        self.turn()
        event, = self.events()
        self.assertEqual(event["route"], "fallback")
        self.assertEqual(event["label"], fallback + " fallback-model")


class SessionBannerTests(unittest.TestCase):
    def test_documented_pattern_captures_real_codex_banner(self):
        # Banner witnessed on the writer host on 2026-09-21.
        banner = "session id: 01a0c612-78f5-7b21-8cfd-f4e3a7bf72e6"
        document = (ROOT / "docs/config.md").read_text()
        pattern = re.search(r"implementer_session = '([^']+)'", document)[1]
        self.assertEqual(re.search(pattern, banner)[1],
                         "01a0c612-78f5-7b21-8cfd-f4e3a7bf72e6")


class SeatProbeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "repo"
        self.repo.mkdir()
        self.tgt = bare_target(self, self.repo)
        self.git("init", "-q")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-qm", "probe base")
        self.sha = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True)

    def configure(self, seat, script):
        self.tgt = bare_target(self, self.repo)
        command = shlex.join([sys.executable, "-c", script])
        self.tgt.config_path.write_text(
            f"[agents]\n{seat} = {json.dumps(command)}\n")

    def test_review_seats_require_command_output(self):
        scripts = (
            ("print('ready')", False),
            ("import subprocess, sys; "
             "assert 'git rev-parse HEAD' in sys.argv[-1]; "
             "print('ready', subprocess.check_output("
             "['git', 'rev-parse', 'HEAD'], text=True).strip())", True),
            ("import subprocess; "
             "print('ready', subprocess.check_output("
             "['git', 'rev-parse', '--short', 'HEAD'], text=True).strip())", False),
        )
        for role, seat in (("review", "reviewer"), ("adjudicate", "adjudicator")):
            for fallback in (False, True):
                for script, passes in scripts:
                    with self.subTest(role=role, fallback=fallback, script=script):
                        self.configure(seat + ("_fallback" if fallback else ""),
                                       script)
                        result = holophyte.agents.probe_seat(
                            self.tgt, role, fallback=fallback)
                        self.assertEqual(result.ok, passes, result.describe())
                        self.assertNotIn(self.sha, result.command[-1])
                        if passes:
                            self.assertIn(self.sha, result.output)
                        else:
                            self.assertIn("answered without reporting the commit",
                                          result.describe())
                            self.assertIn("| ready", result.describe())

    def test_default_container_review_seats_receive_command_goal(self):
        self.configure("reviewer_fallback", "print('ready')")
        # The default route is probed when a fallback is configured.
        with self.tgt.config_path.open("a") as config:
            config.write('adjudicator_fallback = "false"\n')
        def run_review(**kwargs):
            self.assertIn("git rev-parse HEAD", kwargs["prompt"])
            self.assertNotIn(self.sha, kwargs["prompt"])
            return "ready " + subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=kwargs["repo"], text=True)

        for role in ("review", "adjudicate"):
            with self.subTest(role=role), patch.object(
                    review_runner, "run_review", side_effect=run_review):
                self.assertTrue(holophyte.agents.probe_seat(self.tgt, role).ok)
            with patch.object(review_runner, "run_review", return_value="ready"):
                self.assertFalse(holophyte.agents.probe_seat(self.tgt, role).ok)

    def test_implementer_retains_text_only_goal_and_pass_rule(self):
        for fallback in (False, True):
            for output, code, passes in (("READY", 0, True), ("already", 0, True),
                                         ("ready", 1, False), ("hello", 0, False)):
                with self.subTest(fallback=fallback, output=output, code=code):
                    self.configure(
                        "implementer" + ("_fallback" if fallback else ""),
                        "import sys; "
                        "assert sys.argv[-1] == 'Reply with the single word: ready'; "
                        f"print({output!r}); sys.exit({code})")
                    result = holophyte.agents.probe_seat(
                        self.tgt, "implement", fallback=fallback)
                    self.assertEqual(result.ok, passes, result.describe())


class AgentRouteTests(unittest.TestCase):
    def setUp(self):
        self.worktree = Path("/tmp/holophyte-agent-contract")
        self.tgt = bare_target(self, self.worktree)

    @patch.object(holophyte.agents, "run_capped")
    def test_implementer_uses_claude_opus_at_high_effort(self, run_capped):
        run_capped.return_value = (0, "implemented\n")

        result = holophyte.agents.agent(self.tgt, "implement",
                                        "make the focused change", self.worktree)

        self.assertEqual(result, "implemented")
        run_capped.assert_called_once_with(
            [
                "claude", "-p", "make the focused change",
                "--model", "opus", "--effort", "high",
            ],
            self.worktree, 1800,
        )

    @patch.object(holophyte.agents, "run_capped")
    def test_implementer_budget_is_the_dispatch_timeout_under_the_hard_cap(
        self, run_capped
    ):
        run_capped.return_value = (0, "")

        holophyte.agents.agent(self.tgt, "implement", "goal", self.worktree,
                               timeout=300)
        holophyte.agents.agent(self.tgt, "implement", "goal", self.worktree,
                               timeout=7200)

        self.assertEqual([c.args[2] for c in run_capped.call_args_list],
                         [300, 1800])


    @patch.object(review_runner, "run_review")
    def test_reviewer_uses_containerized_codex_sol(self, run_review):
        run_review.return_value = "VERDICT: APPROVE"
        base = "1" * 40
        candidate = "2" * 40

        result = holophyte.agents.agent(
            self.tgt, "review",
            "review the candidate",
            self.worktree,
            base_sha=base,
            candidate_sha=candidate,
            run_id=340,
        )

        self.assertEqual(result, "VERDICT: APPROVE")
        run_review.assert_called_once_with(
            repo=self.worktree,
            run_id=340,
            base_sha=base,
            candidate_sha=candidate,
            prompt="review the candidate",
            model="gpt-5.6-sol",
            effort="medium",
            profile="codex-sol-medium",
            timeout=1800,
            verdicts=None,
            carry=[],
        )
        self.assertEqual(holophyte.agents.agent_route(self.tgt, "review"),
                         "codex-sol-medium")

    @patch.object(review_runner, "run_review")
    def test_reviewer_runs_the_configured_model_and_effort(self, run_review):
        # `[agents] review_model` / `review_effort` choose the pair the
        # container runs, and the round records the route that actually ran.
        self.tgt.config_path.write_text(
            '[agents]\nreview_model = "gpt-6-astra"\nreview_effort = "medium"\n')
        run_review.return_value = "VERDICT: APPROVE"

        holophyte.agents.agent(self.tgt, "review", "review the candidate",
                               self.worktree, base_sha="1" * 40,
                               candidate_sha="2" * 40)

        kwargs = run_review.call_args.kwargs
        self.assertEqual((kwargs["model"], kwargs["effort"], kwargs["profile"]),
                         ("gpt-6-astra", "medium", "codex-astra-medium"))
        self.assertEqual(holophyte.agents.agent_route(self.tgt, "review"),
                         "codex-astra-medium")
        self.assertEqual(holophyte.agents.agent_route(self.tgt, "adjudicate"),
                         "codex-astra-medium")

    @patch.object(review_runner, "run_review")
    def test_a_reviewer_runner_failure_is_an_infra_failure(self, run_review):
        # The container did not start, so no candidate was judged: the run
        # fails, and fails as the factory's own failure rather than one the
        # ticket is charged for.
        run_review.side_effect = review_runner.ReviewBoundaryError(
            "Codex CLI is not installed")

        with self.assertRaises(holophyte.gates.InfraFailure) as raised:
            holophyte.agents.agent(self.tgt, "review", "review the candidate",
                                   self.worktree, base_sha="1" * 40,
                                   candidate_sha="2" * 40)

        self.assertIn("Codex CLI is not installed", str(raised.exception))

    @patch.object(review_runner, "run_review")
    def test_adjudicator_shares_the_reviewer_route_without_verdict_enforcement(
        self, run_review
    ):
        # A malformed terminal reply has to come back as text so the loop can
        # record it and read it as FAIL, not raise at the review boundary.
        run_review.return_value = "no verdict here"

        result = holophyte.agents.agent(
            self.tgt, "adjudicate",
            "adjudicate the candidate",
            self.worktree,
            base_sha="1" * 40,
            candidate_sha="2" * 40,
        )

        self.assertEqual(result, "no verdict here")
        self.assertEqual(run_review.call_args.kwargs["profile"], "codex-sol-medium")
        self.assertIsNone(run_review.call_args.kwargs["verdicts"])


# An implementer stand-in that behaves like a real `claude -p` session under
# the cap: it starts a child of its own, reports both pids, then outlives any
# budget it is given. Whether the child survives the budget is the whole
# question, so it is read from the process table, not from the stand-in.
SPAWNING_IMPLEMENTER = """\
import os, subprocess, sys, time
child = subprocess.Popen(["sleep", "300"])
print("implementer", os.getpid(), "child", child.pid, flush=True)
time.sleep(300)
"""


def process_gone(pid, wait=5.0):
    """True once `pid` is no longer a live process (a reaped or zombie one is
    gone for this purpose: it can write nothing). Polls up to `wait` seconds
    because an orphaned grandchild is reaped by init a moment after the
    group kill, not in the same instant."""
    import time
    deadline = time.monotonic() + wait
    while True:
        try:
            with open(f"/proc/{pid}/stat") as f:
                state = f.read().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return True
        if state == "Z":
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)


class ImplementerProcessGroupTests(unittest.TestCase):
    """The budget ends the implementer's whole process tree, not just the CLI."""

    def test_a_timed_out_implementer_and_its_child_are_both_reaped(self):
        argv = [sys.executable, "-u", "-c", SPAWNING_IMPLEMENTER]
        with tempfile.TemporaryDirectory() as cwd:
            # The route is named the way an operator names one, through
            # `[agents] implementer`: `agent_command()` reads it from the
            # target's config, which is where `agent()` looks it up.
            target = bare_target(self, cwd)
            target.config_path.write_text(
                "[agents]\nimplementer = %s\n" % json.dumps(shlex.join(argv)))
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                holophyte.agents.agent(target, "implement", "spawn and stall",
                                       Path(cwd), timeout=1)

        # Partial output survives the kill and names the two processes.
        output = raised.exception.output
        self.assertIn("implementer", output)
        words = output.split()
        impl_pid = int(words[words.index("implementer") + 1])
        child_pid = int(words[words.index("child") + 1])

        self.assertTrue(process_gone(impl_pid), f"implementer {impl_pid} alive")
        self.assertTrue(process_gone(child_pid), f"child {child_pid} alive")


class FakeLinear:
    """Stand-in for the board `run_task` is handed and archives its records on."""

    def __init__(self):
        self.states = []
        self.comments = []

    # The board lease label (KO-351): what the loop labelled and unlabelled,
    # per issue, so the stub answers the claim's and the close-out's calls.
    def label_issue(self, issue_id, name):
        self.__dict__.setdefault("labels", {}).setdefault(issue_id, [])
        if name not in self.labels[issue_id]:
            self.labels[issue_id].append(name)

    def unlabel_issue(self, issue_id, name):
        self.__dict__.setdefault("labels", {}).setdefault(issue_id, [])
        self.labels[issue_id] = [n for n in self.labels[issue_id] if n != name]

    def issue_labels(self, issue_id):
        return list(self.__dict__.setdefault("labels", {}).get(issue_id, []))

    def set_state(self, issue_id, state):
        self.states.append((issue_id, state))

    def comment(self, task_id, body):
        self.comments.append((task_id, body))


class ReviewLoopTests(unittest.TestCase):
    """End-to-end control flow of `run_task` over a real throwaway repo, with
    only the agent turns faked: the loop's own git, worktree, verify and merge
    steps run for real, so a preserved branch really is a preserved branch."""

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

        self.worktrees = root / "repo.worktrees"
        self.branch = "task/ko-116-add-a-thing"
        self.wt = self.worktrees / "ko-116-add-a-thing"
        self.tgt = holophyte.target.Target(
            path=self.target, holo_dir=root, store_path=root / "store.db",
            config_path=root / "config.toml", worktrees=self.worktrees)
        self.linear = FakeLinear()
        patcher = patch.dict(sys.modules, {"linear_provider": self.linear})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.events = []
        self.goals = []
        # One "verify" per gate run: `run_verify` resolves `run_capped`, its
        # one subprocess call, in `holophyte.gates`, and `agent()` is faked
        # below, so nothing else in a run reaches it.
        real_capped = holophyte.gates.run_capped

        def spy(*args, **kwargs):
            self.events.append("verify")
            return real_capped(*args, **kwargs)

        patcher = patch.object(holophyte.gates, "run_capped", spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.target),
                              check=True, capture_output=True, text=True).stdout

    def run_task(self, *replies, budget_min=1, **task):
        """Drive one task, answering each review/adjudicate turn in order.

        Extra keyword arguments override fields of the task dict, so a test
        can hand the loop a ticket whose body carries its own contract.
        """
        replies = list(replies)

        def fake_agent(target, role, goal, cwd, *, base_sha=None, conn=None,
                       candidate_sha=None, timeout=None, on_start=None, run_id=None,
                       review_round=None):
            self.events.append(role)
            self.goals.append((role, goal))
            if role != "implement":
                return answer_scope(goal, replies.pop(0))
            n = sum(1 for event in self.events if event == "implement")
            (Path(cwd) / f"change{n}.txt").write_text(f"work {n}\n")
            self.git("add", "-A", cwd=cwd)
            self.git("commit", "-q", "-m", f"work {n}", cwd=cwd)
            return f"committed work {n}"

        with patch.object(holophyte.loop, "agent", fake_agent):
            try:
                return holophyte.loop.run_task(self.tgt, {
                    "id": "KO-116", "title": "add a thing",
                    "verify": "echo ok", "budget_min": budget_min,
                    "contracts": [], **task,
                }, provider=self.linear)
            except holophyte.gates.RunFailure:
                # run_task's failure exits raise so their reasons reach the
                # close-out; a direct call answers False the way main() does.
                return False

    def records(self):
        """The run's records as the ticket archive holds them.

        Linear, not FINDINGS.md: that file is rendered from the store's rows
        at close-out (`tests/test_wiring_findings.py`), and these tests drive
        `run_task()` without a store.
        """
        return "\n".join(body for _, body in self.linear.comments)

    def test_implementer_goal_carries_the_ticket_body_and_verify_commands(self):
        """The implementer works from the approved body, not from the title.

        The phrase asserted on lives only in the body, so a goal built from
        the title alone cannot contain it — the reviewer holds the candidate
        to criteria the implementer would never have seen.
        """
        body = ("## Acceptance criteria\n\n"
                "- [ ] The word used is `unparseable`, never `unreadable`.\n")

        merged = self.run_task("VERDICT: APPROVE", body=body)

        self.assertTrue(merged)
        goal = next(g for role, g in self.goals if role == "implement")
        self.assertIn("The word used is `unparseable`, never `unreadable`.",
                      goal)
        self.assertIn("add a thing", goal)
        self.assertIn("echo ok", goal)
        self.assertIn("Commit messages carry no tool attribution or co-author lines "
                      "for an AI.", goal)

    def test_review_goal_numbers_the_criteria_and_asks_for_a_checklist(self):
        """The reviewer is asked to account for each criterion by number; a
        task with none gets no such block, so the prompt never asks for a
        checklist the loop would not read."""
        self.run_task("CRITERION 1: met \u2014 t\nCRITERION 2: met \u2014 u\n"
                      "VERDICT: APPROVE",
                      criteria=["Given a, then b", "Given c, then d"])
        with_criteria = next(g for role, g in self.goals if role == "review")
        self.assertIn("1. Given a, then b", with_criteria)
        self.assertIn("2. Given c, then d", with_criteria)
        self.assertIn("CRITERION n: met", with_criteria)
        self.assertIn("Name tests as `tests/file.py::TestClass::test_name`; "
                      "the loop checks the test exists.", with_criteria)

        self.goals.clear()
        merged = self.run_task("VERDICT: APPROVE")
        self.assertTrue(merged)
        without = next(g for role, g in self.goals if role == "review")
        self.assertNotIn("CRITERION", without)

    def test_review_and_adjudication_goals_carry_the_ticket_body(self):
        """The reviewer and the adjudicator judge against the approved body.

        Both turns used to be told "against the task: <title>", so a criterion
        the implementer had to meet was never in front of the reviewer who was
        meant to hold the candidate to it. The phrase lives only in the body.
        """
        body = ("## Acceptance criteria\n\n"
                "- [ ] The rendered line contains `unparseable`.\n")

        merged = self.run_task("VERDICT: REQUEST_CHANGES",
                               "VERDICT: REQUEST_CHANGES",
                               "Small and complete.\nVERDICT: PASS", body=body)

        self.assertTrue(merged)
        judged = [(role, g) for role, g in self.goals
                  if role in ("review", "adjudicate")]
        self.assertEqual([role for role, _ in judged],
                         ["review", "review", "adjudicate"])
        # The two fix rounds are implement turns too: an implementer addressing
        # findings is a fresh process and needs the contract just as much.
        fix_rounds = [g for role, g in self.goals if role == "implement"][1:]
        self.assertEqual(len(fix_rounds), 2)
        for role, goal in judged + [("fix round", g) for g in fix_rounds]:
            with self.subTest(role=role):
                self.assertIn("The rendered line contains `unparseable`.", goal)
                self.assertIn("add a thing", goal)

    def test_round_two_findings_get_a_fix_round_then_adjudication(self):
        merged = self.run_task("VERDICT: REQUEST_CHANGES",
                               "VERDICT: REQUEST_CHANGES",
                               "Small and complete.\nVERDICT: PASS")

        self.assertTrue(merged)
        # Round 2's findings buy a third implementer turn, and the verify gate
        # runs over that fix commit before the adjudicator is dispatched.
        self.assertEqual(self.events, [
            "implement",
            "verify", "review", "implement",
            "verify", "review", "implement",
            "verify", "adjudicate",
            "verify",  # pre-merge
        ])

    def test_terminal_pass_merges_and_leaves_the_linear_state_alone(self):
        """The merge is `run_task()`'s; the ticket's Linear state is not.

        Ticket status lives in the store and is projected onto Linear by
        `main()` through `mirror_push()`, so a run that merges makes no state
        call of its own — a direct one here would be a second writer of the
        same fact, from a frame with no store to be right about."""
        merged = self.run_task("VERDICT: REQUEST_CHANGES",
                               "VERDICT: REQUEST_CHANGES",
                               "VERDICT: PASS")

        self.assertTrue(merged)
        self.assertIn(f"Merge {self.branch}", self.git("log", "--format=%s", "main"))
        self.assertNotIn(self.branch, self.git("branch", "--list", self.branch))
        self.assertFalse(self.wt.exists())
        self.assertEqual(self.linear.states, [])

    def test_close_out_records_actual_duration_estimate_and_rounds(self):
        # Claim at t=100 s, close-out 42.7 s later: 0.711 min, reported to one
        # decimal, against a 20 min estimate and a single review round.
        with patch.object(holophyte.loop, "monotonic", side_effect=[100.0, 142.7]):
            merged = self.run_task("VERDICT: APPROVE", budget_min=20)

        self.assertTrue(merged)
        timing = "actual: 0.7 min · estimate: 20 min · rounds: 1"
        self.assertIn(timing, self.records())

    def test_terminal_fail_preserves_the_branch_and_stops(self):
        merged = self.run_task("VERDICT: REQUEST_CHANGES",
                               "VERDICT: REQUEST_CHANGES",
                               "Broken.\nVERDICT: FAIL")

        self.assertFalse(merged)
        self.assertNotIn("Merge ", self.git("log", "--format=%s", "main"))
        self.assertIn(self.branch, self.git("branch", "--list", self.branch))
        self.assertTrue(self.wt.exists())
        # No round-3 fix: the last turn dispatched was the adjudicator.
        self.assertEqual(self.events[-1], "adjudicate")
        self.assertIn("Terminal adjudication", self.records())
        self.assertIn("VERDICT: FAIL", self.records())

    def test_malformed_terminal_verdict_is_a_preserved_fail(self):
        merged = self.run_task("VERDICT: REQUEST_CHANGES",
                               "VERDICT: REQUEST_CHANGES",
                               "1. tests are thin\n2. rename the helper")

        self.assertFalse(merged)
        self.assertNotIn("Merge ", self.git("log", "--format=%s", "main"))
        self.assertIn(self.branch, self.git("branch", "--list", self.branch))
        self.assertTrue(self.wt.exists())
        self.assertEqual(self.events[-1], "adjudicate")
        self.assertIn("MALFORMED", self.records())
        self.assertIn("2. rename the helper", self.records())


class RowWriteSanitizationTests(unittest.TestCase):
    """Agent text is sanitized where a row is written, not where a file is
    appended: FINDINGS.md is rendered from those rows now, so an escape
    sequence or a heading that reached one would come back on every render.
    Asserted over the message `raw_finding()` stores rather than over the
    helper, since the helper is only useful if the write sites go through it —
    `parse_findings()` builds its messages through the same one."""

    def stored(self, entry):
        return holophyte.review.raw_finding(entry)["message"]

    def test_ansi_escapes_and_control_bytes_are_stripped(self):
        # A coloured tool trace of the shape that reached the KO-107 entry.
        written = self.stored("\x1b[0m\x1b[32m$ \x1b[0mgit status\x07\r\n"
                              "\x1bOn branch main\n"
                              "VERDICT: APPROVE")

        self.assertNotIn("\x1b", written)
        self.assertNotIn("\x07", written)
        self.assertNotIn("\r", written)
        self.assertIn("$ git status\n", written)
        self.assertIn("On branch main\n", written)
        self.assertIn("VERDICT: APPROVE", written)

    def test_embedded_headings_are_demoted_out_of_the_files_outline(self):
        written = self.stored("## Blockers\n\n1. the migration is missing\n\n"
                              "### Detail\n\nVERDICT: REQUEST_CHANGES")

        # A stored message contributes nothing to the rendered file's outline.
        self.assertEqual([ln for ln in written.splitlines()
                          if ln.startswith("#")], [])
        self.assertIn("**Blockers**", written)
        self.assertIn("**Detail**", written)

    def test_an_oversize_block_is_truncated_with_a_visible_marker(self):
        entry = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
        self.assertGreater(len(entry), 10_000)

        written = self.stored(entry)

        self.assertIn("[… truncated]", written)
        self.assertIn("line 0 ", written)
        self.assertNotIn("line 199 ", written)
        self.assertLessEqual(len(written), holophyte.review.MAX_FINDING_CHARS)

    def test_c1_escape_sequences_are_stripped_with_their_payload(self):
        # A CSI introduced by the single C1 byte, not by ESC-[: dropping only
        # the introducer would leave `31m` printing as literal text.
        written = self.stored("\x9b31mred\x9b0m\x85 tail\nVERDICT: APPROVE")

        self.assertNotIn("\x9b", written)
        self.assertNotIn("31m", written)
        self.assertNotIn("\x85", written)
        self.assertIn("red tail\n", written)

    def test_indented_and_setext_headings_are_demoted_too(self):
        written = self.stored("   ## Indented blocker\n\n"
                              "Setext blocker\n==============\n\n"
                              "Second one\n---\n\nVERDICT: REQUEST_CHANGES")

        outline = [ln for ln in written.splitlines()
                   if ln.lstrip().startswith("#") or set(ln.strip()) in ({"="}, {"-"})]
        self.assertEqual(outline, [])
        self.assertIn("**Indented blocker**", written)
        self.assertIn("**Setext blocker**", written)
        self.assertIn("**Second one**", written)

    def test_truncation_keeps_the_trailing_verdict_line(self):
        # The verdict is the outcome the entry is evidence for, and it sits at
        # the end — exactly where a head-only truncation would drop it.
        entry = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
        entry += "\nVERDICT: REQUEST_CHANGES"

        written = self.stored(entry)

        self.assertIn("[… truncated]", written)
        self.assertNotIn("line 199 ", written)
        self.assertEqual(written.splitlines()[-1], "VERDICT: REQUEST_CHANGES")

    def test_truncation_stays_within_budget_when_it_keeps_a_verdict(self):
        entry = "x" * 10_000 + "\nVERDICT: APPROVE"

        body = self.stored(entry)

        self.assertLessEqual(len(body), holophyte.review.MAX_FINDING_CHARS)

    def test_an_oversize_verdict_line_cannot_escape_the_budget(self):
        # A malformed adjudicator reply is persisted verbatim, so the trailing
        # line the truncation branch must keep is agent-written and unbounded.
        entry = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
        entry += "\nVERDICT: " + "y" * 10_000

        body = self.stored(entry)

        self.assertLessEqual(len(body), holophyte.review.MAX_FINDING_CHARS)
        self.assertIn("[… truncated]", body)
        self.assertNotIn("line 199 ", body)
        # The verdict is still recorded, cut rather than dropped.
        self.assertTrue(body.splitlines()[-1].startswith("VERDICT: yyy"))

    def test_clean_text_is_written_through_unchanged(self):
        entry = ("Round 1: REQUEST_CHANGES -> fix round\n"
                 "- `store.py:99`: no migration, so init() leaves #42 broken\n"
                 "\nVERDICT: REQUEST_CHANGES")

        self.assertEqual(self.stored(entry), entry)


if __name__ == "__main__":
    unittest.main()
