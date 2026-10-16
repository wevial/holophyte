import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402
    APPROVE,
    REQUEST_CHANGES,
    FakeAgent,
    no_agent_processes,
)
from loop_fixture import BRANCH, LoopFixture, StubProvider, a_task  # noqa: E402

import holophyte.board.projection  # noqa: E402
import holophyte.cli.operator  # noqa: E402
import holophyte.loop.adjudicate  # noqa: E402
import holophyte.loop.review_round  # noqa: E402
import store  # noqa: E402

WEEKLY_LIMIT = "You've hit your weekly limit · resets 11am (UTC)"


class UsageLimitStopTests(LoopFixture):
    def test_weekly_limit_is_infra_and_preserves_wip_without_spending_an_attempt(self):
        script, _ = self.script(
            "claude-implementer.sh",
            'echo "unfinished work" > unfinished.txt\n'
            f'echo "{WEEKLY_LIMIT}"\nexit 1\n')
        self.configure(f'[agents]\nimplementer = "{script}"\n')
        self.run_scripts()

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

    def script(self, name, *turns):
        state = self.db.parent / f"{name}-state"
        state.mkdir()
        script = self.db.parent / name
        cases = "".join(f"{n})\n{body};;\n" for n, body in enumerate(turns, 1))
        script.write_text(
            '#!/bin/sh\nset -e\n'
            'for last; do :; done\n'
            'case "$last" in "Reply with the single word: ready") '
            f'echo probe >> "{state}/calls"; echo ready; exit 0;; esac\n'
            f'n=$(( $(cat "{state}/turns" 2>/dev/null || echo 0) + 1 ))\n'
            f'echo "$n" > "{state}/turns"\n'
            f'echo turn >> "{state}/calls"\n'
            f'git rev-parse HEAD > "{state}/head-$n"\n'
            f'git branch --show-current > "{state}/branch-$n"\n'
            f'printf "%s\\n" "$@" > "{state}/args-$n"\n'
            f'case "$n" in\n{cases}*) exit 97;; esac\n')
        script.chmod(0o755)
        return script, state

    def run_scripts(self, *reviews):
        provider = StubProvider(a_task())
        reviewer = FakeAgent(*reviews)
        with no_agent_processes(), \
                patch.dict(sys.modules, {"linear_provider": provider}), \
                patch.object(holophyte.loop.review_round, "agent", reviewer), \
                patch.object(holophyte.loop.adjudicate, "agent", reviewer):
            holophyte.cli.operator.main(self.project, provider)
        return reviewer

    def test_requeue_starts_on_preserved_wip_and_merges_both_commits(self):
        script, state = self.script(
            "claude-harness",
            'echo "unfinished work" > unfinished.txt\n'
            f'echo "{WEEKLY_LIMIT}"\nexit 1\n',
            'echo "finished work" > finished.txt\ngit add finished.txt\n'
            'git commit -q -m "finish preserved work"\n')
        self.configure(f'[agents]\nimplementer = "{script}"\n')
        self.run_scripts()
        wip = self.git("rev-parse", BRANCH).strip()
        holophyte.cli.operator.requeue(
            self.project, "KO-131", "route restored", out=io.StringIO())

        self.run_scripts(APPROVE)

        self.assertEqual((state / "head-2").read_text().strip(), wip)
        self.assertEqual((state / "branch-2").read_text().strip(), BRANCH)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("failed",), ("merged",)])
        self.assertIn(wip, self.git("rev-list", "main").splitlines())
        self.assertIn("finish preserved work", self.subjects())
        self.assertEqual(self.git("show", "main:unfinished.txt"), "unfinished work\n")
        self.assertEqual(self.git("show", "main:finished.txt"), "finished work\n")

    def test_weekly_limit_in_fix_probes_fallback_and_reviews_its_commit(self):
        script, primary = self.script(
            "claude-harness",
            'echo candidate > candidate.txt\ngit add candidate.txt\n'
            'git commit -q -m candidate\necho "session id: primary-session"\n',
            f'echo "{WEEKLY_LIMIT}"\nexit 1\n',
            f'echo "{WEEKLY_LIMIT}"\nexit 1\n')
        fallback, secondary = self.script(
            "fallback",
            'echo fixed > fixed.txt\ngit add fixed.txt\n'
            'git commit -q -m "ADDRESS: complete the candidate"\n')
        self.configure(
            f'[agents]\nimplementer = "{script}"\n'
            f'implementer_fallback = "{fallback}"\n'
            "implementer_session = 'session id: ([a-z-]+)'\n"
            f'implementer_resume = "{script} --resume {{session}}"\n'
            '[loop]\nfix_session = "resume"\n')

        reviewer = self.run_scripts(REQUEST_CHANGES, APPROVE)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual((primary / "turns").read_text().strip(), "3")
        self.assertIn("--resume\nprimary-session", (primary / "args-2").read_text())
        self.assertEqual((secondary / "calls").read_text().splitlines(),
                         ["probe", "turn"])
        ((summary,),) = self.read(
            "SELECT summary FROM runEvents WHERE kind = 'route_fallback'")
        self.assertEqual(json.loads(summary)["reason"], WEEKLY_LIMIT)
        self.assertEqual(reviewer.roles, ["review", "review"])
        fixed = self.git("log", "main", "--format=%H", "--", "fixed.txt").strip()
        self.assertEqual(fixed, reviewer.turns[1].candidate_sha)
        self.assertEqual(self.git("show", "main:fixed.txt"), "fixed\n")

    def test_fix_without_tokens_is_infra_and_preserves_candidate(self):
        self.check_fix_exit({"input_tokens": 0, "output_tokens": 0}, "infra")

    def test_fix_using_tokens_remains_a_work_failure(self):
        self.check_fix_exit({"input_tokens": 1, "output_tokens": 0}, "fix_no_progress")

    def test_fix_without_usage_remains_a_work_failure(self):
        self.check_fix_exit(None, "fix_no_progress")

    def check_fix_exit(self, usage, expected):
        result = json.dumps({"type": "result", "result": "unable to continue",
                             "usage": usage})
        body = (f"echo '{result}'\n" if usage is not None
                else "echo unable to continue\n")
        script, state = self.script(
            "claude-harness",
            'echo candidate > candidate.txt\ngit add candidate.txt\n'
            'git commit -q -m candidate\n', body + 'exit 1\n')
        self.configure('[agents.implementer]\nharness = "claude"\n'
                       f'[harnesses]\nclaude = "{script}"\n')

        self.run_scripts(REQUEST_CHANGES)

        outcome_class = "infra" if expected == "infra" else "work"
        self.assertEqual(self.read(
            "SELECT outcome, failureKind, outcomeClass FROM runs"),
            [("failed", expected, outcome_class)])
        candidate = (state / "head-2").read_text().strip()
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), candidate)
        self.assertEqual(self.git("show", f"{BRANCH}:candidate.txt"), "candidate\n")
        self.assertEqual(self.read("SELECT COUNT(*) FROM reviewRounds"), [(1,)])
        if expected == "infra":
            ((reason,),) = self.read("SELECT outcomeReason FROM runs")
            self.assertEqual(reason, "fix turn exited 1 without using a token; "
                             f"branch {BRANCH} preserved at {candidate}")
