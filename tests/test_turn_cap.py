from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    REQUEST_CHANGES,
    Commit,
    FakeAgent,
    _git,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    CommitThenTimeout,
    LoopFixture,
    StubProvider,
    a_task,
)
from sweep_fixture import MINUTE, SweepTestCase  # noqa: E402

import holophyte.agents.roles  # noqa: E402
import holophyte.config.checks  # noqa: E402
import holophyte.config.project  # noqa: E402
from holophyte.loop import implement  # noqa: E402


class RealImplementDispatch(FakeAgent):
    def __init__(self, *script):
        super().__init__(*script)
        self.armed = []

    def __call__(self, target, role, goal, cwd, **kwargs):
        if role != "implement":
            return super().__call__(target, role, goal, cwd, **kwargs)

        def run_capped(argv, cwd, timeout, **_):
            self.armed.append(timeout)
            (Path(cwd) / "work.txt").write_text("work\n")
            _git(Path(cwd), "add", "-A")
            _git(Path(cwd), "commit", "-q", "-m", "work")
            return 0, "committed"

        with patch.object(holophyte.agents.roles, "run_capped", run_capped):
            return holophyte.agents.roles.agent(target, role, goal, cwd, **kwargs)


class TurnCapLoopTests(LoopFixture):
    def timeouts(self):
        return [json.loads(payload) for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'turn_timeout'")]

    def test_a_ninety_minute_box_arms_the_real_dispatch_with_ninety_minutes(self):
        fake = RealImplementDispatch(APPROVE)

        self.loop(fake=fake, provider=StubProvider(dict(a_task(), budget_min=90)))

        self.assertEqual(fake.armed, [5400])

    def test_a_turn_cap_timeout_names_the_cap_and_its_seconds(self):
        self.configure("[agents]\nturn_cap_min = 20\n")

        fake, _ = self.loop(CommitThenTimeout("late work"),
                            provider=StubProvider(dict(a_task(), budget_min=90)))

        self.assertEqual(fake.turns[0].timeout, 1200)
        ((kind, reason),) = self.read(
            "SELECT failureKind, outcomeReason FROM runs")
        self.assertEqual(kind, "budget")
        self.assertTrue(reason.startswith(
            "implementer exceeded the 1200 s turn cap ([agents] turn_cap_min"
            " = 20) under a 90 min box; work kept on "), reason)
        self.assertEqual(self.timeouts(), [
            {"role": "implement", "limit": "turn_cap", "seconds": 1200}])

    def test_a_time_box_timeout_names_the_box(self):
        self.loop(CommitThenTimeout("late work"),
                  provider=StubProvider(dict(a_task(), budget_min=5)))

        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertTrue(reason.startswith(
            "implementer exceeded the 5 min budget; work kept on "), reason)
        self.assertEqual(self.timeouts(), [
            {"role": "implement", "limit": "time_box", "seconds": 300}])

    def fix_turn_timeout(self, budget_min):
        self.configure("[agents]\nturn_cap_min = 20\n")
        fake, _ = self.loop(
            Commit("work"), REQUEST_CHANGES, Commit("the fix"), APPROVE,
            provider=StubProvider(dict(a_task(), budget_min=budget_min)))
        self.assertEqual(fake.roles,
                         ["implement", "review", "implement", "review"])
        return fake.turns[2].timeout

    def test_a_fix_turn_on_a_long_box_is_clipped_to_the_turn_cap(self):
        self.assertEqual(self.fix_turn_timeout(90), 1200)

    def test_a_fix_turn_under_the_cap_asks_for_its_full_box(self):
        self.assertEqual(self.fix_turn_timeout(15), 900)

    def test_startup_refuses_a_turn_cap_that_is_not_whole_minutes(self):
        for value in ("0", '"90"', "true"):
            with self.subTest(value=value):
                self.configure(f"[agents]\nturn_cap_min = {value}\n")
                with self.assertRaisesRegex(SystemExit,
                                            r"\[agents\] turn_cap_min must be"):
                    holophyte.config.checks.check_config(self.project)


class ImplementArmingTests(SweepTestCase):
    def armed(self, budget_min, worked_min):
        run = self.a_run(budget_min=budget_min)
        self.conn.execute("UPDATE runs SET workingMs = ? WHERE id = ?",
                          (worked_min * MINUTE, run))
        self.conn.commit()
        return implement.implement_arming(self.project, self.conn, run,
                                          budget_min)

    def test_the_implement_turn_asks_for_the_box_less_its_agent_work(self):
        for budget_min, worked_min, asked in ((90, 0, 5400), (90, 30, 3600),
                                              (90, 85, 600), (5, 4, 300)):
            with self.subTest(budget_min=budget_min, worked_min=worked_min):
                self.assertEqual(self.armed(budget_min, worked_min)[0], asked)

    def test_a_floored_turn_under_a_lower_cap_names_the_time_box(self):
        config = self.db.parent / "config.toml"
        config.write_text("[agents]\nturn_cap_min = 5\n")
        self.project = holophyte.config.project.Project.locate(self.target)

        self.assertEqual(self.armed(90, 90), (300, "time_box"))
