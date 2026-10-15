"""A check red with a new failure after its fix gets another check fix round."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # factory.py imports store by name
sys.path.insert(0, str(HERE))  # `discover -s tests` and `-m unittest` alike
import babysit_fixture as cases  # noqa: E402
from fake_agent import APPROVE, Commit, Idle  # noqa: E402
from loop_fixture import MergeModeFixture, StubProvider, a_task  # noqa: E402

import holophyte.pr.github  # noqa: E402 - after the sys.path insert above


class BabysitCheckRoundsTests(cases.BabysitHelpers, MergeModeFixture):
    UNIT = {"name": "unit", "status": "completed", "conclusion": "failure",
            "html_url": "https://github.com/example/repo/actions/runs/5/job/42",
            "id": 42, "app": {"slug": "github-actions"}}

    def red_per_head(self, *logs):
        """`UNIT` red on the n-th head with `logs[n]` as its job log, a new
        job id on every read; green on every head past the last log."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route()
        self.enterContext(patch.object(holophyte.pr.github, "SLEEP"))
        heads, ids = [], iter(range(42, 10**4))
        def check_runs(target, pull, sha):
            if sha not in heads:
                heads.append(sha)
                if len(heads) <= len(logs):
                    self.job_log.write_text(logs[len(heads) - 1])
            if len(heads) > len(logs):
                return [dict(self.UNIT, conclusion="success")]
            return [dict(self.UNIT, id=next(ids))]
        self.enterContext(patch("holophyte.pr.pr_status._check_runs_of", check_runs))

    def events(self, kind):
        return [summary for (summary,) in self.read(
            f"SELECT summary FROM runEvents WHERE kind = '{kind}' ORDER BY id")]

    def test_a_new_failure_on_the_fix_commit_gets_a_second_round_and_merges(self):
        self.red_per_head("FAIL: test_a (tests.test_y.Case.test_a)\n",
                          "FAIL: test_b (tests.test_y.Case.test_b)\n")
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: test_a"), Commit("fix: test_b"),
                            APPROVE, Idle(""),
                            provider=StubProvider(dict(a_task(), body=self.BODY)))
        fixes = [turn for turn in fake.turns[3:] if turn.role == "implement"
                 and "CHECK unit" in turn.goal]
        self.assertEqual(len(fixes), 2, self.question())
        self.assertIn("FAIL: test_b", fixes[1].goal)
        self.assertIn("check fix round 2 of 2", fixes[1].goal)
        fixed = self.pushed()[-1][1]
        self.assertIn("fix: test_b", self.git("log", "-1", "--format=%s", fixed))
        self.assertEqual([v["sha"] for kind, v in self.api_calls()
                          if kind == "merge"], [fixed])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(len(self.events("check_fix_round")), 2)
        self.assertIn("Check fix round 2 of 2", " ".join(
            text for (text,) in self.read(
                "SELECT text FROM ledger WHERE kind = 'round'")))


if __name__ == "__main__":
    unittest.main()
