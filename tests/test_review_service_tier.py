"""The container reviewer hands Codex a configured service tier, and only then.

Run: python3 -m unittest discover -s tests -p 'test_review_service_tier.py' -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import holophyte.config.project  # noqa: E402 - after the sys.path insert above
import review_runner  # noqa: E402 - after the sys.path insert above
from holophyte.agents import roles  # noqa: E402
from holophyte.config.checks import check_config  # noqa: E402

# `run` stands in for the container: it runs the script's own Codex statement
# under /bin/sh with the positional arguments `docker run` was handed, the
# toolchain mount's host side standing in for /opt/codex/bin.
DOCKER = """#!{python}
import os, sys
args = sys.argv[1:]
if args[0] == "inspect":
    sys.exit(1)
if args[0] != "run":
    sys.exit(0)
toolchain, = [a.split(":")[0] for a in args if a.endswith(":/opt/codex/bin:ro")]
shell = args.index("/bin/sh")
script, positional = args[shell + 3], args[shell + 4:]
statement = script[script.index("exec /opt/codex/bin/codex"):]
statement = statement.replace("/opt/codex/bin", toolchain)
print("PREFLIGHT_OK candidate=shim", file=sys.stderr, flush=True)
os.execv("/bin/sh", ["/bin/sh", "-eu", "-c", statement, *positional])
"""

CODEX = """#!{python}
import json, os, sys
with open(os.environ["HOLOPHYTE_CODEX_ARGV"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
for item in ({{"type": "command_execution", "exit_code": 0}},
             {{"type": "agent_message", "text": "VERDICT: APPROVE"}}):
    print(json.dumps({{"type": "item.completed", "item": item}}))
"""

GOAL = "judge the candidate"
FALLBACK = ('review_fallback_model = "gpt-5.6-sol"\n'
            'review_fallback_effort = "medium"\n')


class ReviewServiceTierTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.target = self.root / "repo"
        self.target.mkdir()
        git = ["git", "-c", "user.name=Test", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git, "init", "-q", "-b", "main"], cwd=self.target,
                       check=True)
        (self.target / "value.txt").write_text("candidate\n")
        subprocess.run([*git, "add", "value.txt"], cwd=self.target, check=True)
        subprocess.run([*git, "commit", "-qm", "candidate"], cwd=self.target,
                       check=True)
        self.sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.target, text=True).strip()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, body in (("docker", DOCKER), ("codex", CODEX),
                           ("codex-code-mode-host", "#!/bin/sh\nexit 0\n")):
            (bin_dir / name).write_text(body.format(python=sys.executable))
            (bin_dir / name).chmod(0o755)
        (self.root / "auth.json").write_text("{}")
        self.argv_log = self.root / "codex-argv"
        env = patch.dict(os.environ, {
            "HOLOPHYTE_HOME": str(self.root / "home"),
            "HOLOPHYTE_CODEX_ARGV": str(self.argv_log),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"})
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("SCRATCH_ROOT", self.root / "reviews"),
                            ("CODEX_AUTH", self.root / "auth.json")):
            patcher = patch.object(review_runner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def configure(self, agents):
        config = holophyte.config.project.state_dir(self.target) / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("[agents]\n" + agents)
        self.project = holophyte.config.project.Project.locate(self.target)

    def codex_argv(self, *, fallback=False):
        """Run one container review and return the argv the stub Codex got."""
        self.argv_log.unlink(missing_ok=True)
        roles.container_review(self.project, "review", GOAL, self.target,
                               self.sha, self.sha, None, None, fallback)
        runs = self.argv_log.read_text().splitlines()
        self.assertEqual(len(runs), 1, runs)
        return json.loads(runs[0])

    def test_a_configured_tier_follows_the_effort_argument(self):
        self.configure('review_model = "gpt-6.1-sol"\nreview_effort = "high"\n'
                       'review_service_tier = "priority"\n')

        argv = self.codex_argv()

        effort = argv.index('model_reasoning_effort="high"')
        self.assertEqual(argv[effort - 1:effort + 3],
                         ["-c", 'model_reasoning_effort="high"',
                          "-c", 'service_tier="priority"'])
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")

    def test_without_a_tier_codex_runs_exactly_as_before(self):
        self.configure("")

        self.assertEqual(self.codex_argv(), [
            "exec", "--json", "-C", "/home/reviewer/candidate",
            "-m", "gpt-6-astra", "-c", 'model_reasoning_effort="high"',
            "-s", "danger-full-access", "--disable", "multi_agent",
            GOAL])

    def test_an_empty_or_non_string_tier_is_refused_naming_the_key(self):
        for key, value in (("review_service_tier", '""'),
                           ("review_service_tier", '"  "'),
                           ("review_service_tier", "3"),
                           ("review_service_tier", "true"),
                           ("review_fallback_service_tier", '""'),
                           ("review_fallback_service_tier", '["priority"]')):
            with self.subTest(key=key, value=value):
                self.configure(f"{FALLBACK}{key} = {value}\n")
                with self.assertRaisesRegex(SystemExit,
                                            rf"\[agents\] {key} must be"):
                    check_config(self.project)

    def test_only_the_fallback_route_carries_the_fallback_tier(self):
        self.configure(FALLBACK + 'review_fallback_service_tier = "priority"\n')

        primary = self.codex_argv()
        fallback = self.codex_argv(fallback=True)

        self.assertEqual(primary[primary.index("-m") + 1], "gpt-6-astra")
        self.assertFalse([arg for arg in primary if "service_tier" in arg],
                         primary)
        self.assertEqual(fallback[fallback.index("-m") + 1], "gpt-5.6-sol")
        effort = fallback.index('model_reasoning_effort="medium"')
        self.assertEqual(fallback[effort + 1:effort + 3],
                         ["-c", 'service_tier="priority"'])

    def test_a_tier_with_quotes_and_metacharacters_reaches_codex_as_one_argument(
            self):
        mark = self.root / "shell-ran"
        tier = (f'fast" # $(touch {mark}) `touch {mark}`; touch {mark} * \'x'
                ' \\ \u00e9')
        self.configure(f"review_service_tier = {json.dumps(tier)}\n")

        argv = self.codex_argv()

        override, = [arg for arg in argv if arg.startswith("service_tier=")]
        self.assertEqual(argv[argv.index(override) - 1], "-c")
        self.assertEqual(tomllib.loads(override), {"service_tier": tier})
        self.assertEqual(argv[-1], GOAL)
        self.assertFalse(mark.exists())


if __name__ == "__main__":
    unittest.main()
