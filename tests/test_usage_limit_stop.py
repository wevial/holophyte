import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import no_agent_processes  # noqa: E402
from loop_fixture import BRANCH, LoopFixture, StubProvider, a_task  # noqa: E402

import holophyte.board.projection  # noqa: E402
import holophyte.cli.operator  # noqa: E402
import store  # noqa: E402

WEEKLY_LIMIT = "You've hit your weekly limit · resets 11am (UTC)"


class UsageLimitStopTests(LoopFixture):
    def test_weekly_limit_is_infra_and_preserves_wip_without_spending_an_attempt(self):
        script = self.db.parent / "claude-implementer.sh"
        script.write_text(
            '#!/bin/sh\n'
            'for last; do :; done\n'
            'case "$last" in "Reply with the single word: ready") '
            'echo ready; exit 0;; esac\n'
            'echo "unfinished work" > unfinished.txt\n'
            f'echo "{WEEKLY_LIMIT}"\n'
            'exit 1\n')
        script.chmod(0o755)
        self.configure(f'[agents]\nimplementer = "{script}"\n')
        provider = StubProvider(a_task())
        with no_agent_processes(), \
                patch.dict(sys.modules, {"linear_provider": provider}):
            holophyte.cli.operator.main(self.project, provider)

        ((summary,),) = self.read(
            "SELECT summary FROM runEvents WHERE kind = 'wip_committed'")
        wip = summary.rsplit(" at ", 1)[1]
        self.assertEqual(self.git("show", f"{wip}:unfinished.txt"),
                         "unfinished work\n")
        self.assertEqual(self.git("rev-list", "--count", f"main..{wip}").strip(), "1")
        with self.subTest("route outage classification"):
            self.assertEqual(self.read(
                "SELECT outcome, failureKind, outcomeClass FROM runs"),
                [("failed", "infra", "infra")])
        with self.subTest("outage reason"):
            ((reason,),) = self.read("SELECT outcomeReason FROM runs")
            self.assertIn(WEEKLY_LIMIT, reason)
        with self.subTest("work preserved"):
            self.assertIn(BRANCH, self.branches())
            self.assertEqual(self.git("rev-parse", BRANCH),
                             self.git("rev-parse", wip))
            self.assertTrue((self.worktrees / "ko-131-add-a-thing").is_dir())
        with self.subTest("no attempt spent"):
            conn = store.open(str(self.db))
            self.addCleanup(conn.close)
            self.assertEqual(holophyte.board.projection.failure_history(conn, 1), [])
