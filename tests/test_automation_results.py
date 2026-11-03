"""The main watch settles an act firing whose lease expired and reads its
rerun's result: real stores, real host sweep passes, and the factory's real
`gh api` path against a `gh` on PATH that logs each method and path.

Run: python3 -m unittest discover -s tests -p 'test_automation_results.py' -v
"""
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import holophyte.automation.step as step
import store.automations
from tests.main_watch_fixture import job, sweep_pass
from tests.test_automation_act import ActFixture, S

S2 = "9d8c7b6a5f4e3f8a6c1b0d2e4f6a8c0b1d3e5f70"
G = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
EXPIRED = 6 * 60 * 1000


class ResultFixture(ActFixture):
    def setUp(self):
        super().setUp()
        self.clock = int(time.time() * 1000)

    def pass_at(self, at):
        """One pass at `at`; the argv of each worker it would start."""
        argvs = []

        def spawn(argv, **_kwargs):
            argvs.append(argv)
            return SimpleNamespace(pid=0)
        with patch.object(step, "SPAWN", spawn), \
                patch.object(step, "systemd_runner", lambda: None):
            sweep_pass(at)
        return argvs

    def later(self, step_ms=EXPIRED):
        self.clock += step_ms
        return self.pass_at(self.clock)

    def taken_and_dead(self, name):
        """A leased firing whose worker recorded its pid and then died."""
        self.recorded_pass()
        firing = self.only(name)
        store.automations.take_firing(self.conn(name), firing.id, 999_999,
                                      now=int(time.time() * 1000))
        return firing.id

    def answer(self, name, key, value):
        self.act_github.answer(self.paths[name], key, value)

    def head(self, name, sha, runs):
        repo = self.repos[name]
        self.answer(name, f"GET repos/{repo}/commits/main", {"sha": sha})
        self.answer(name, f"GET repos/{repo}/commits/{sha}/check-runs",
                    {"total_count": len(runs), "check_runs": runs})

    def rerun_answers(self, name, runs):
        repo = self.repos[name]
        self.answer(name, f"GET repos/{repo}/commits/{S}/check-runs",
                    {"total_count": len(runs), "check_runs": runs})

    def compares(self, name):
        return [call for call in self.act_github.calls(self.paths[name])
                if "/compare/" in call]

    def patrol(self, name):
        code, err, out = self.holo_out("patrol", "-p", name)
        self.assertEqual(code, 0, err)
        snap = json.loads(self.holo_out("patrol", "-p", name, "--json")[2])
        [row] = snap["projects"]
        return out, row


class ExpiredLeaseTests(ResultFixture):
    def test_an_untaken_firing_is_replayed_once(self):
        self.act("alpha")
        [first] = self.recorded_pass()

        [replay] = self.later()

        firing = self.only("alpha")
        self.assertEqual(replay, first)
        self.assertEqual(firing.state, "leased")
        self.assertEqual(firing.detail["replays"], 1)

        self.assertEqual(self.later(), [])

        firing = self.only("alpha")
        self.assertEqual(firing.state, "canceled")
        self.assertIn("after one replay", firing.reason)
        self.assertEqual(self.later(), [])
        self.assertEqual(self.rerun_posts("alpha"), [])

    def test_a_landed_rerun_is_read_back_as_confirmed(self):
        self.act("alpha")
        self.taken_and_dead("alpha")
        self.answer("alpha", "GET repos/o/alpha/actions/runs/7",
                    {"id": 7, "run_attempt": 2})

        self.assertEqual(self.later(), [])

        firing = self.only("alpha")
        self.assertEqual(firing.state, "confirmed")
        self.assertIs(firing.detail["read_back"], True)
        self.assertEqual(firing.detail["rerun_attempt"], 2)
        self.assertEqual(self.act_github.calls(self.paths["alpha"], "POST"),
                         [])

    def test_an_unsent_or_unreadable_rerun_is_never_retried(self):
        for name in self.NAMES:
            self.act(name)
        self.recorded_pass()
        for name in self.NAMES:
            store.automations.take_firing(
                self.conn(name), self.only(name).id, 999_999,
                now=int(time.time() * 1000))
        read = "GET repos/o/alpha/actions/runs/7"
        self.answer("alpha", read, None)
        self.answer("alpha", f"EXEC BEFORE {read}",
                    "sys.exit(print('gh: Bad Gateway (HTTP 502)',"
                    " file=sys.stderr) or 1)")

        self.assertEqual(self.later(), [])
        self.assertEqual(self.later(), [])

        canceled, unknown = self.only("beta"), self.only("alpha")
        self.assertEqual(canceled.state, "canceled")
        self.assertIn("before its rerun reached GitHub", canceled.reason)
        self.assertEqual(unknown.state, "unknown")
        self.assertIn("502", unknown.reason)
        for name in self.NAMES:
            self.assertEqual(
                self.act_github.calls(self.paths[name], "POST"), [])
        out, row = self.patrol("alpha")
        self.assertIn(f"  automation main_watch unknown {S[:12]}: ", out)
        self.assertFalse(row["clean"])


class RerunResultTests(ResultFixture):
    def confirmed(self, *names):
        """Real workers rerun main's failed jobs; each firing is confirmed."""
        self.assertEqual(self.started_pass(), [0] * len(names))
        for name in names:
            firing = self.only(name)
            self.assertEqual((firing.state, firing.detail["jobs"]),
                             ("confirmed", [{"name": "unit", "job_id": 11}]))
        return firing

    def test_a_rerun_that_passes_is_a_suspected_flake(self):
        self.act("alpha")
        self.confirmed("alpha")
        self.rerun_answers("alpha", [job("unit", "success", 12, 7)])

        self.later()

        firing = self.only("alpha")
        self.assertEqual(firing.detail["result"], "green")
        self.assertTrue(firing.reason.startswith("suspected flake: unit"),
                        firing.reason)
        out, _row = self.patrol("alpha")
        self.assertNotIn("automation main_watch", out)

    def test_a_rerun_that_fails_names_the_merges_since_green(self):
        self.act("alpha")
        self.head("alpha", G, [job("unit", "success", 5, 3)])
        self.assertEqual(self.recorded_pass(), [])
        self.assertEqual(self.state()["main_green"], {"alpha": G})
        self.head("alpha", S, [job("unit", "failure", 11, 7)])
        self.act("beta")
        self.confirmed(*self.NAMES)
        self.answer("alpha", f"GET repos/o/alpha/compare/{G}...{S}", {
            "commits": [{"sha": "a1", "commit": {"message": "feat: one"}},
                        {"sha": "b2",
                         "commit": {"message": "fix: two\n\nbody"}}]})
        for name in self.NAMES:
            self.rerun_answers(name, [job("unit", "failure", 12, 7)])

        self.later()

        alpha, beta = self.only("alpha"), self.only("beta")
        self.assertEqual((alpha.detail["result"], beta.detail["result"]),
                         ("red_again", "red_again"))
        self.assertIn("unit", alpha.reason)
        self.assertIn(f"merged since {G[:12]}: a1 feat: one, b2 fix: two",
                      alpha.reason)
        self.assertTrue(beta.reason.endswith("no green main seen yet"),
                        beta.reason)
        self.assertEqual(len(self.compares("alpha")), 1)
        self.assertEqual(self.compares("beta"), [])
        out, row = self.patrol("alpha")
        self.assertIn(f"  automation main_watch red again {S[:12]}: ", out)
        self.assertFalse(row["clean"])

    def test_a_pending_rerun_waits_and_a_moved_head_supersedes_it(self):
        self.act("alpha")
        before = self.confirmed("alpha")
        self.rerun_answers("alpha", [job("unit", None, 12, 7,
                                         status="in_progress")])

        self.later()

        self.assertEqual(self.only("alpha"), before)

        self.head("alpha", S2, [job("unit", None, 21, 8,
                                    status="in_progress")])
        self.later()

        firing = self.only("alpha")
        self.assertEqual(firing.detail["result"], "superseded")
        self.assertIn(S2[:12], firing.reason)

    def test_the_reruns_own_failure_triggers_nothing(self):
        self.act("alpha")
        self.confirmed("alpha")
        self.rerun_answers("alpha", [job("unit", "failure", 12, 7)])
        self.later()
        self.assertEqual(self.only("alpha").detail["result"], "red_again")

        self.assertEqual(self.later(), [])
        self.assertEqual(self.later(), [])

        [firing] = self.firings("alpha")
        self.assertEqual(firing.detail["result"], "red_again")
        self.assertEqual(len(self.rerun_posts("alpha")), 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
