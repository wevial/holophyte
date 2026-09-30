"""An implementer turn killed by a signal keeps its edits and runs once more."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, FakeAgent, no_agent_processes  # noqa: E402
from loop_fixture import BRANCH, LoopFixture, StubProvider, a_task  # noqa: E402

import holophyte.cli.operator  # noqa: E402
import holophyte.loop.adjudicate  # noqa: E402
import holophyte.loop.review_round  # noqa: E402

EDIT = 'echo "crash-time edit" > crashed-edit.txt\n'
CRASH = 'echo "panic: Illegal instruction (core dumped)"\nkill -ILL $$\n'
COMMIT = ('echo "finished" > finished.txt\ngit add finished.txt\n'
          'git commit -q -m "finished work"\n')
WIP = "WIP: implementer crashed mid-edit (KO-131); not verified"


class ImplementerCrashTests(LoopFixture):
    def run_loop(self, *turns, reviews=(), task=None, config=""):
        """Run the loop with a real implementer script whose Nth turn runs
        `turns[N-1]`; each turn's arguments are kept in `turn-N`."""
        self.state = self.db.parent / "implementer-state"
        self.state.mkdir()
        cases = "".join(f"{n})\n{body};;\n" for n, body in enumerate(turns, 1))
        script = self.db.parent / "implementer.sh"
        script.write_text(
            "#!/bin/sh\nulimit -c 0\n"
            'for last; do :; done\n'
            'case "$last" in "Reply with the single word: ready") '
            "echo ready; exit 0;; esac\n"
            f'n=$(( $(cat "{self.state}/turns" 2>/dev/null || echo 0) + 1 ))\n'
            f'echo "$n" > "{self.state}/turns"\n'
            f'printf "%s\\n" "$@" > "{self.state}/turn-$n"\n'
            f'case "$n" in\n{cases}esac\n')
        script.chmod(0o755)
        self.configure(f'[agents]\nimplementer = "{script}"\n{config}')
        provider = StubProvider(task or a_task())
        reviewer = FakeAgent(*reviews)
        with no_agent_processes(), \
                patch.dict(sys.modules, {"linear_provider": provider}), \
                patch.object(holophyte.loop.review_round, "agent", reviewer), \
                patch.object(holophyte.loop.adjudicate, "agent", reviewer):
            holophyte.cli.operator.main(self.project, provider)
        return script

    def turn_args(self, n):
        return (self.state / f"turn-{n}").read_text()

    def turns_started(self):
        return int((self.state / "turns").read_text())

    def crash_events(self):
        return [(summary, json.loads(payload)) for summary, payload in self.read(
            "SELECT summary, payload FROM runEvents WHERE kind = 'crash'")]

    def test_a_crashed_turn_is_kept_as_wip_and_retried_fresh_through_to_merge(self):
        self.run_loop(EDIT + CRASH, COMMIT, reviews=(APPROVE,))

        self.assertEqual(self.turns_started(), 2)
        ((summary, payload),) = self.crash_events()
        self.assertEqual(payload["exit_status"], -4)
        self.assertIn("-4", summary)
        self.assertIn("Illegal instruction", payload["output"])
        retry = self.turn_args(2)
        self.assertIn("killed by a signal", retry)
        self.assertIn("A WIP commit", retry)
        self.assertIn("Implement this task", retry)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        subjects = self.subjects()
        self.assertIn(WIP, subjects)
        self.assertIn("finished work", subjects)
        self.assertEqual(self.git("show", "main:crashed-edit.txt"),
                         "crash-time edit\n")

    def test_a_crashed_turn_with_a_recorded_session_resumes_it(self):
        script = self.db.parent / "implementer.sh"
        self.run_loop('echo "session id: first-session"\n' + EDIT + CRASH, COMMIT,
                      reviews=(APPROVE,),
                      config="implementer_session = 'session id: ([a-z-]+)'\n"
                             f'implementer_resume = "{script} --resume {{session}}"\n')

        retry = self.turn_args(2).splitlines()
        self.assertEqual(retry[:2], ["--resume", "first-session"])
        self.assertIn("A WIP commit", self.turn_args(2))
        self.assertNotIn("Implement this task", self.turn_args(2))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_second_crash_fails_the_run_and_keeps_the_branch(self):
        self.run_loop(EDIT + CRASH,
                      'echo "second edit" > second-edit.txt\n' + CRASH)

        self.assertEqual(self.turns_started(), 2)
        self.assertEqual(len(self.crash_events()), 2)
        ((outcome, reason),) = self.read("SELECT outcome, outcomeReason FROM runs")
        self.assertEqual(outcome, "failed")
        self.assertTrue(reason.startswith("implementer crashed twice"), reason)
        self.assertIn(BRANCH, self.branches())
        self.assertTrue((self.worktrees / "ko-131-add-a-thing").is_dir())
        self.assertEqual(self.git("log", f"main..{BRANCH}", "--format=%s")
                         .splitlines(), [WIP, WIP])
        self.assertEqual(self.git("show", f"{BRANCH}:crashed-edit.txt"),
                         "crash-time edit\n")
        self.assertEqual(self.git("show", f"{BRANCH}:second-edit.txt"),
                         "second edit\n")

    def test_an_ordinary_nonzero_exit_is_not_retried_and_is_discarded(self):
        self.run_loop(EDIT + "echo giving up\nexit 1\n")

        self.assertEqual(self.turns_started(), 1)
        self.assertEqual(self.crash_events(), [])
        self.assertEqual(self.read("SELECT outcome, failureKind FROM runs"),
                         [("failed", "no_commits")])
        self.assertNotIn(BRANCH, self.branches())
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())

    def test_a_budget_kill_takes_the_timed_out_path_without_a_crash_retry(self):
        self.run_loop(EDIT + "sleep 600\n", task=dict(a_task(), budget_min=0.05))

        self.assertEqual(self.turns_started(), 1)
        self.assertEqual(self.crash_events(), [])
        self.assertEqual(self.read("SELECT outcome, failureKind FROM runs"),
                         [("failed", "budget")])
        (subject,) = self.git("log", f"main..{BRANCH}", "--format=%s").splitlines()
        self.assertTrue(subject.startswith("WIP: implementer budget fired"), subject)
