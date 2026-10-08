"""A fake `claude` on PATH answering the failure-cause question as told."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from holophyte import question_cli
from tests.fake_agent import AGENT_BINARIES, SpawnGuard
from tests.test_question_backends import FAKE, claude_output

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

    def down(self):
        for mode in ("probe", "question"):
            self.reply(mode, "", exit_code=1)

    def calls(self):
        log = self.bin / "claude.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]
