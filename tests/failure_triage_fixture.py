"""A fake `claude` on PATH answering the failure-cause question as told."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from holophyte import question_cli
from holophyte.agents import probes, roles
from holophyte.agents.probes import PROBE_GOAL
from tests.fake_agent import AGENT_BINARIES, FakeAgent, SpawnGuard
from tests.test_question_backends import FAKE, claude_output, codex_output

FAILURES = '[questions.failures]\nbackend = "claude"\n'
REQUEUE = FAILURES + "requeue = true\n"


def question_guard():
    """The loop's spawn guard with `claude` allowed: PATH resolves it to the fake."""
    return SpawnGuard(blocked=tuple(b for b in AGENT_BINARIES if b != "claude"))


class FakeClaude:
    def __init__(self, case):
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.bin = Path(tmp.name)
        path = self.bin / "claude"
        path.write_text(FAKE)
        path.chmod(0o755)
        self.reply("probe", claude_output("ready", 0.99))
        case.enterContext(patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"}))
        question_cli.ROUTES.clear()
        case.addCleanup(question_cli.ROUTES.clear)

    def reply(self, mode, output, exit_code=0):
        (self.bin / f"claude.{mode}.out").write_text(output)
        (self.bin / f"claude.{mode}.exit").write_text(str(exit_code))

    def answer(self, choice, confidence):
        self.reply("question", claude_output(choice, confidence))

    def codex_fallback(self, choice, confidence):
        path = self.bin / "codex"
        path.write_text(FAKE)
        path.chmod(0o755)
        (self.bin / "codex.probe.out").write_text(codex_output("ready", 0.99))
        (self.bin / "codex.question.out").write_text(codex_output(choice, confidence))

    def down(self):
        for mode in ("probe", "question"):
            self.reply(mode, "", exit_code=1)

    def calls(self):
        log = self.bin / "claude.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]


class ProfileTurns:
    """Implement and fix turns through the real role dispatch, so their
    `agent_turn` labels name the route the run resolved, played from the
    script's implement steps; the rest from a FakeAgent. Every profile probe
    passes."""

    def __init__(self, *script):
        self.implements = [step for step in script if step.role == "implement"]
        self.others = FakeAgent(*[step for step in script
                                  if step.role != "implement"])
        self.goals = []

    def runner(self, cmd, cwd, timeout, **kwargs):
        if cmd[-1] == PROBE_GOAL:
            return 0, "ready"
        return 0, self.implements.pop(0).play(Path(cwd), len(self.goals))

    def __call__(self, target, role, goal, cwd, **kwargs):
        if role != "implement":
            return self.others(target, role, goal, cwd, **kwargs)
        self.goals.append(goal)
        return roles.agent(target, role, goal, cwd, **kwargs)

    def __enter__(self):
        self.patches = (patch.object(roles, "run_capped", self.runner),
                        patch.object(probes, "run_capped", self.runner))
        for each in self.patches:
            each.start()
        return self

    def __exit__(self, *exc):
        for each in self.patches:
            each.stop()
