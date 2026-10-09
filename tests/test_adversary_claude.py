"""The adversary's Claude family: its container turn, alternation and outages."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_adversary  # noqa: E402
from fake_agent import (  # noqa: E402
    ADVERSARY,
    APPROVE,
    REQUEST_CHANGES,
    FakeAgent,
)
from loop_fixture import StubProvider, a_task  # noqa: E402
from test_review_runner import docker_shim, two_commit_repo  # noqa: E402

import review_runner  # noqa: E402
from holophyte.agents import probes, roles  # noqa: E402
from holophyte.agents.agent_routes import reset  # noqa: E402
from holophyte.config.checks import check_config  # noqa: E402

Change, attack, finding = (test_adversary.Change, test_adversary.attack,
                           test_adversary.finding)
ON = test_adversary.ON
KEY = "HOLOPHYTE_TEST_ADVERSARY_KEY"
CREDENTIAL = f'[agents]\nadversary_credential = {{ env = "{KEY}" }}\n'
CLAUDE = {"harness": "claude", "model": "opus", "effort": "high",
          "credential": KEY}
SECRET = "holophyte-secret-token-value"
LIMIT = json.dumps({"type": "result", "is_error": True,
                    "result": "You've hit your limit · resets 5pm"})

SHIM = """#!{python}
import json, os, sys
with open(os.environ["HOLOPHYTE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "secret_in_env":
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == {secret!r}}}) + "\\n")
if sys.argv[1] == "run":
    sys.stderr.write("PREFLIGHT_OK candidate=test\\n")
    sys.stdout.write(open(os.environ["HOLOPHYTE_DOCKER_REPLY"]).read())
    sys.exit(int(os.environ.get("HOLOPHYTE_DOCKER_EXIT", "0")))
sys.exit(1 if sys.argv[1] == "inspect" else 0)
"""


class ClaudeTurnTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        bin_dir, self.env = docker_shim(self.root)
        (bin_dir / "docker").write_text(
            SHIM.format(python=sys.executable, secret=SECRET))
        (self.root / "auth.json").write_text("{}")
        self.env.update(HOLOPHYTE_DOCKER_REPLY=str(self.root / "reply"),
                        CLAUDE_CODE_OAUTH_TOKEN=SECRET)
        self.base, self.candidate = two_commit_repo(self.root / "repo")

    def review(self, reply, exit_status=0, **route):
        (self.root / "reply").write_text(reply)
        env = dict(self.env, HOLOPHYTE_DOCKER_EXIT=str(exit_status))
        with patch.dict(os.environ, env), \
                patch.object(review_runner, "SCRATCH_ROOT", self.root / "reviews"), \
                patch.object(review_runner, "CODEX_AUTH", self.root / "auth.json"):
            return review_runner.run_review(
                repo=self.root / "repo", base_sha=self.base,
                candidate_sha=self.candidate, prompt="attack it", verdicts=None,
                **route)

    def runs(self):
        lines = Path(self.env["HOLOPHYTE_DOCKER_LOG"]).read_text().splitlines()
        return [json.loads(line) for line in lines
                if json.loads(line)["argv"][0] == "run"]

    def test_a_claude_turn_runs_the_cli_in_the_hardened_container(self):
        codex_reply = "".join(json.dumps({"type": "item.completed", "item": item})
                              + "\n" for item in (
            {"type": "command_execution", "exit_code": 0},
            {"type": "agent_message", "text": "codex said"}))
        self.review(codex_reply)
        reply = self.review(
            json.dumps({"type": "result", "is_error": False,
                        "result": "Nothing broke.\nADVERSARY: DONE"}),
            harness="claude", model="opus", effort="high",
            credential="CLAUDE_CODE_OAUTH_TOKEN")

        self.assertEqual(reply, "Nothing broke.\nADVERSARY: DONE")
        codex, claude = self.runs()
        self.assertTrue(claude["secret_in_env"])
        argv = claude["argv"]
        image = argv.index(review_runner.IMAGE)
        flags = [arg for arg in argv[:image] if arg.startswith("--")]
        self.assertIn("--env=CLAUDE_CODE_OAUTH_TOKEN", flags)
        self.assertEqual(
            [flag for flag in flags if flag != "--env=CLAUDE_CODE_OAUTH_TOKEN"],
            [arg for arg in codex["argv"][:image] if arg.startswith("--")])
        for flag in review_runner.hardening_flags(os.getuid(), os.getgid()):
            self.assertIn(flag, flags)
        mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--volume"]
        self.assertTrue(any(m.endswith(":/workspace:ro") for m in mounts), mounts)
        self.assertFalse(any(SECRET in arg for arg in argv + codex["argv"]))

        script = argv[argv.index("-c") + 1].splitlines()
        run = next(i for i, line in enumerate(script) if line.startswith("exec "))
        self.assertEqual(script[run - 1], "cd /home/reviewer/candidate")
        positional = dict(zip(("$1", "$2", "$3"), argv[argv.index("-c") + 3:]))
        command = [positional.get(arg, arg) for arg in shlex.split(script[run])[1:]]
        self.assertEqual(command[:2], ["/opt/claude/bin/claude", "-p"])
        for flag, value in (("--model", "opus"), ("--effort", "high"),
                            ("--output-format", "json")):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(command[-1], "attack it")

    def test_a_claude_turn_needs_no_codex_release_or_auth_file(self):
        for name in review_runner.CODEX_FILES:
            (self.root / "bin" / name).unlink()
        (self.root / "auth.json").unlink()
        reply = self.review(json.dumps({"is_error": False, "result": "done"}),
                            harness="claude", model="opus", effort="high",
                            credential="CLAUDE_CODE_OAUTH_TOKEN")
        self.assertEqual(reply, "done")

    def test_text_that_is_not_json_or_an_error_result_is_a_boundary_error(self):
        for label, output, status in (
                ("not json", "x" * 3000 + "\nthe CLI crashed", 0),
                ("is_error", LIMIT, 1)):
            with self.subTest(label):
                with self.assertRaises(review_runner.ReviewBoundaryError) as e:
                    self.review(output, status, harness="claude", model="opus",
                                effort="high",
                                credential="CLAUDE_CODE_OAUTH_TOKEN")
                self.assertEqual(e.exception.tail,
                                 output[-review_runner.EVIDENCE_TAIL:])


class RealAdversary(FakeAgent):
    def __call__(self, target, role, goal, cwd, **kwargs):
        if role == ADVERSARY:
            return roles.agent(target, role, goal, cwd, **kwargs)
        return super().__call__(target, role, goal, cwd, **kwargs)


def limit_failure():
    try:
        review_runner.parse_claude_output(LIMIT, None)
    except review_runner.ReviewBoundaryError as error:
        return error
    raise AssertionError("a limit reply parsed")


class FamilyTests(test_adversary.AdversaryFixture):
    def rounds(self):
        return [(run, json.loads(payload)) for run, payload in self.read(
            "SELECT runId, payload FROM runEvents WHERE kind = 'adversary_round'"
            " ORDER BY seq")]

    def routes_of(self, fake):
        return [turn.family_route for turn in self.turns(fake, ADVERSARY)]

    def test_odd_runs_start_on_claude_and_each_later_pass_flips_family(self):
        self.configure(ON + CREDENTIAL)
        odd, _ = self.loop(Change("poetry.lock"), APPROVE, attack(),
                           provider=StubProvider(a_task(1)))
        concern = finding("src/app.py", 4, "a symlink may slip by", "concern")
        even, _ = self.loop(Change("poetry.lock", "v2\n"), REQUEST_CHANGES,
                            attack(concern), attack(),
                            Change("poetry.lock", "v3\n"), APPROVE,
                            provider=StubProvider(a_task(2)))

        rounds = self.rounds()
        self.assertEqual([(run % 2, event["round"]) for run, event in rounds],
                         [(1, 1), (0, 1), (0, 2)])
        self.assertEqual([(e["family"], e["model"], e["effort"])
                          for _, e in rounds],
                         [("claude", "opus", "high"),
                          ("codex", "gpt-6-astra", "high"),
                          ("claude", "opus", "high")])
        self.assertFalse(any("family_reason" in e for _, e in rounds))
        self.assertEqual(self.routes_of(odd), [CLAUDE])
        self.assertEqual(self.routes_of(even), [None, CLAUDE])

    def test_codex_is_forced_without_a_credential_and_a_set_seat_is_configured(
            self):
        self.configure(ON)
        self.loop(Change("poetry.lock"), APPROVE, attack(),
                  provider=StubProvider(a_task(1)))
        self.configure(ON + CREDENTIAL + f'adversary = "{sys.executable}"\n')
        self.loop(Change("poetry.lock", "v2\n"), APPROVE, attack(),
                  provider=StubProvider(a_task(2)))

        (first_run, forced), (_, configured) = self.rounds()
        self.assertEqual(first_run % 2, 1)
        self.assertEqual((forced["family"], forced["family_reason"]),
                         ("codex", "no adversary_credential"))
        self.assertEqual(configured["family"], "configured")

    def test_an_unset_credential_fails_the_route_before_any_container(self):
        self.configure(ON + CREDENTIAL)
        environment = {name: value for name, value in os.environ.items()
                       if name != KEY}
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), \
                contextlib.redirect_stdout(output):
            _, guard = self.loop(fake=RealAdversary(Change("poetry.lock"),
                                                    APPROVE))

        self.assertEqual(guard.spawned, [])
        self.assertEqual(self.read("SELECT outcome, failureKind FROM runs"),
                         [("failed", "review_route")])
        self.assertIn(f"Claude credential variable {KEY} is unset",
                      output.getvalue())

    def test_an_unset_credential_never_switches_to_the_container_fallback_pair(
            self):
        self.configure(ON + CREDENTIAL + 'review_fallback_model = "gpt-6-luna"\n'
                       'review_fallback_effort = "high"\n')
        self.addCleanup(reset, self.project)
        real = review_runner.run_review

        def run_review(*, prompt, candidate_sha, **kwargs):
            if prompt == probes.REVIEW_PROBE_GOAL:
                return f"ready {candidate_sha}"
            return real(prompt=prompt, candidate_sha=candidate_sha, **kwargs)
        environment = {name: value for name, value in os.environ.items()
                       if name != KEY}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(review_runner, "run_review", side_effect=run_review), \
                contextlib.redirect_stdout(io.StringIO()):
            _, guard = self.loop(fake=RealAdversary(Change("poetry.lock"),
                                                    APPROVE))

        self.assertEqual(guard.spawned, [])
        self.assertEqual(self.read("SELECT outcome, failureKind FROM runs"),
                         [("failed", "review_route")])
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM runEvents WHERE kind = 'route_fallback'"),
            [(0,)])

    def test_a_claude_pass_keeps_its_credential_out_of_the_record(self):
        secret = "holophyte-adversary-credential-value"
        self.configure(ON + CREDENTIAL)
        leak = finding("src/app.py", 4, f"the token is {secret}", "concern")
        with patch.dict(os.environ, {KEY: secret}):
            self.loop(Change("poetry.lock"), APPROVE, attack(leak))

        [(_, event)] = self.rounds()
        self.assertEqual(event["family"], "claude")
        recorded = [text for (text,) in self.read(
            "SELECT payload FROM runEvents WHERE payload IS NOT NULL"
            " UNION ALL SELECT text FROM ledger")]
        self.assertTrue(any("the token is" in text for text in recorded))
        self.assertFalse(any(secret in text for text in recorded))

    def adversary_turns(self):
        return [json.loads(payload) for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'agent_turn'"
            " ORDER BY seq") if json.loads(payload)["role"] == ADVERSARY]

    def scripted_container(self, calls):
        def run_review(*, prompt, candidate_sha, harness="codex", **kwargs):
            calls.append((harness, prompt[:31]))
            if prompt == probes.REVIEW_PROBE_GOAL:
                return f"ready {candidate_sha}"
            if harness == "claude":
                raise limit_failure()
            return "Nothing broke.\nADVERSARY: DONE"
        return patch.object(roles.review_runner, "run_review",
                            side_effect=run_review)

    def test_a_claude_limit_switches_to_the_probed_adversary_fallback(self):
        log = self.db.parent / "fallback-calls"
        script = self.db.parent / "adversary-fallback"
        script.write_text(
            f"#!{sys.executable}\nimport subprocess, sys\n"
            f"open({str(log)!r}, 'a').write(sys.argv[-1][:31] + '\\n')\n"
            f"if sys.argv[-1] == {probes.REVIEW_PROBE_GOAL!r}:\n"
            " print('ready ' + subprocess.check_output("
            "['git', 'rev-parse', 'HEAD'], text=True).strip())\n"
            "else:\n print('Nothing broke.\\nADVERSARY: DONE')\n")
        script.chmod(0o755)
        self.configure(ON + CREDENTIAL + f'adversary_fallback = "{script}"\n')
        self.addCleanup(reset, self.project)
        calls = []
        with self.scripted_container(calls), \
                contextlib.redirect_stdout(io.StringIO()):
            self.loop(fake=RealAdversary(Change("poetry.lock"), APPROVE))

        [switch] = [json.loads(summary) for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'route_fallback'")]
        self.assertEqual(switch["seat"], "adversary")
        self.assertIn("You've hit your limit", switch["reason"])
        self.assertEqual(log.read_text().splitlines(),
                         [probes.REVIEW_PROBE_GOAL[:31],
                          "You are a READ-ONLY adversarial"])
        self.assertEqual([harness for harness, goal in calls
                          if goal != probes.REVIEW_PROBE_GOAL[:31]], ["claude"])
        self.assertEqual([(t["label"], t["route"]) for t in self.adversary_turns()],
                         [("claude opus", "primary"), (str(script), "fallback")])
        [(_, event)] = self.rounds()
        self.assertEqual(event["family"], "fallback")
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_claude_limit_with_no_fallback_fails_and_runs_no_codex_turn(self):
        self.configure(ON + CREDENTIAL)
        calls = []
        with self.scripted_container(calls), \
                contextlib.redirect_stdout(io.StringIO()):
            self.loop(fake=RealAdversary(Change("poetry.lock"), APPROVE))

        self.assertEqual([harness for harness, _ in calls], ["claude"])
        self.assertEqual(self.read("SELECT outcome, failureKind FROM runs"),
                         [("failed", "review_route")])
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM runEvents WHERE kind = 'route_fallback'"),
            [(0,)])


class CredentialConfigTests(test_adversary.AdversaryFixture):
    def test_only_the_env_form_naming_a_variable_passes_the_startup_check(self):
        self.configure('[agents]\nadversary_credential = '
                       '{ env = "CLAUDE_CODE_OAUTH_TOKEN" }\n')
        check_config(self.project)
        for value in ('{ file = "x" }', '{ env = "1BAD" }',
                      '"CLAUDE_CODE_OAUTH_TOKEN"'):
            with self.subTest(value=value):
                self.configure(f"[agents]\nadversary_credential = {value}\n")
                with self.assertRaises(SystemExit) as e:
                    check_config(self.project)
                self.assertIn("adversary_credential", str(e.exception))


class RealContainerTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
                         "set HOLOPHYTE_TEST_DOCKER=1 for container integration")
    def test_the_claude_script_reaches_authentication_in_the_real_image(self):
        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        review_runner._ensure_image(review_runner.IMAGE,
                                    review_runner.DOCKERFILE.read_text(),
                                    candidate="HEAD")
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        base, candidate = two_commit_repo(root / "repo")
        staged = review_runner.stage_candidate(root / "repo", root / "stage",
                                               base, candidate)
        home, toolchain = review_runner._prepare_runtime(
            root, review_runner.CODEX_AUTH, None)
        name = f"{review_runner.CONTAINER_PREFIX}{root.name.replace('.', '-')}"
        command = review_runner.container_command(
            image=review_runner.IMAGE, workspace=staged.path,
            reviewer_home=home, toolchain=toolchain, name=name,
            prompt="Say hello.", uid=os.getuid(), gid=os.getgid(),
            model="opus", effort="high", harness="claude", credential=KEY)
        self.addCleanup(review_runner._remove_container, name)
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=300, env=dict(os.environ, **{KEY: ""}))

        self.assertIn("PREFLIGHT_OK", result.stderr, result.stderr)
        for broken in ("not found", "No such file", "Read-only file system",
                       "Permission denied", "EACCES", "EROFS"):
            self.assertNotIn(broken, result.stdout + result.stderr)
        reply = json.loads(result.stdout)
        self.assertTrue(reply["is_error"], reply)
        self.assertRegex(reply["result"], re.compile(r"log ?in|auth|api key", re.I))


if __name__ == "__main__":
    unittest.main()
