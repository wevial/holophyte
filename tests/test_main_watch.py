"""The main watch in the host sweep: real stores under a temporary home, the
factory's real `gh` call path against a `gh` on PATH answering main's reads.

Run: python3 -m unittest discover -s tests -p 'test_main_watch.py' -v
"""
import store
import store.automations
from holophyte.config.checks import check_config
from tests.main_watch_fixture import FakeMainGitHub, automations, job, sweep_pass
from tests.test_sweep_host import MINUTE, T0, HostSweepFixture

S = "5e1f0c2a9b7d4e3f8a6c1b0d2e4f6a8c0b1d3e5f"
S2 = "9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c"


class MainWatchFixture(HostSweepFixture):
    NAMES = ("alpha", "beta", "gamma")

    def setUp(self):
        super().setUp()
        self.github = FakeMainGitHub(self, self.root / "gh")
        for name in self.NAMES:
            self.github.origin(self.paths[name])

    def firings(self, name):
        return store.automations.firings(self.conn(name))

    def red(self, name, sha=S, run=7):
        self.github.main(self.paths[name], sha, [
            job("unit", "failure", 11, run), job("docs", "success", 12, run)])


class MainWatchTests(MainWatchFixture):
    def test_a_red_required_check_on_main_is_proposed_once(self):
        automations(self.paths["alpha"], "dry_run")
        self.red("alpha")

        sweep_pass(T0)

        [first] = self.firings("alpha")
        self.assertEqual((first.automation, first.state, first.mode),
                         ("main_watch", "proposed", "dry_run"))
        self.assertEqual(first.detail, {"sha": S, "workflow_run_id": 7,
                                        "jobs": [{"name": "unit",
                                                  "job_id": 11}]})
        self.assertIn("would rerun the failed jobs of workflow run 7",
                      first.reason)

        sweep_pass(T0 + MINUTE)
        self.assertEqual(self.firings("alpha"), [first])

        self.github.main(self.paths["alpha"], S2,
                         [job("unit", "failure", 21, 8)])
        sweep_pass(T0 + 2 * MINUTE)
        newest, oldest = self.firings("alpha")
        self.assertEqual(oldest, first)
        self.assertEqual((newest.detail["sha"], newest.detail["workflow_run_id"]),
                         (S2, 8))
        self.assertIn("workflow run 8", newest.reason)

    def test_an_optional_or_pending_main_reserves_nothing(self):
        for name in ("alpha", "beta"):
            automations(self.paths[name], "dry_run")
        self.github.main(self.paths["alpha"], S, [
            job("unit", "success", 11, 7), job("docs", "failure", 12, 7)])
        self.github.main(self.paths["beta"], S, [
            job("unit", "in_progress", 11, 7, status="in_progress")])

        sweep_pass(T0)

        for name in ("alpha", "beta"):
            self.assertEqual(len(self.github.head_reads(self.paths[name])), 1)
            self.assertEqual(self.firings(name), [])

    def test_an_off_project_is_never_read(self):
        automations(self.paths["beta"], "off")
        automations(self.paths["gamma"], "dry_run")
        for name in self.NAMES:
            self.red(name)

        sweep_pass(T0)

        for name in ("alpha", "beta"):
            self.assertEqual(self.firings(name), [])
            self.assertEqual(self.github.head_reads(self.paths[name]), [])
        self.assertEqual(len(self.firings("gamma")), 1)

    def test_a_wrong_mode_is_off_for_the_sweep_and_refused_at_startup(self):
        automations(self.paths["alpha"], "always")
        self.red("alpha")
        silent = self.a_run("alpha")

        sweep_pass(T0 + 6 * MINUTE)
        printed = sweep_pass(T0 + 12 * MINUTE)

        self.assertEqual(self.conn("alpha").execute(
            "SELECT phase, outcome FROM runs WHERE id = ?",
            (silent,)).fetchone(), ("failed", "failed"))
        named = [line for line in printed.splitlines()
                 if "[automations.main_watch] mode" in line]
        self.assertEqual(len(named), 1, printed)
        self.assertEqual(self.firings("alpha"), [])
        with self.assertRaises(SystemExit) as refused:
            check_config(self.target("alpha"))
        self.assertIn("[automations.main_watch] mode", str(refused.exception))


class AutomationCursorTests(MainWatchFixture):
    NAMES = ("alpha", "beta")

    def test_a_cut_project_moves_the_automation_cursor(self):
        """`sweep_sec = 8` gives the step two seconds, one for each project;
        alpha's head read takes longer, so its next read is refused."""
        with open(self.home / "host.toml", "a") as registry:
            registry.write("\n[supervisor]\nsweep_sec = 8\n")
        for name in self.NAMES:
            automations(self.paths[name], "dry_run")
            self.red(name)
        self.github.main(self.paths["alpha"], S,
                         [job("unit", "failure", 11, 7)], head_sleep=1.5)

        printed = sweep_pass(T0)

        self.assertIn("[holo2] alpha: automations cut, share spent before",
                      printed)
        self.assertEqual(self.firings("alpha"), [])
        self.assertEqual(self.state()["automation_cursor"], "beta")
