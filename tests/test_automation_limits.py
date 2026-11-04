"""An automation's act limits and its self-pause: real stores, real host sweep
passes and real `factory.py --automation-firing` workers, whose GitHub calls
go through the factory's `gh api` path to a `gh` on PATH.

Run: python3 -m unittest discover -s tests -p 'test_automation_limits.py' -v
"""
import datetime
import json
import subprocess
import time
import zoneinfo
from unittest.mock import patch

import holophyte.automation.step as step
import store
import store.automations
from holophyte.host.registry import HOST_FILE
from tests.main_watch_fixture import automations, job, sweep_pass
from tests.test_automation_act import S
from tests.test_automation_results import ResultFixture
from tests.test_sweep_host import MINUTE, T0

HOUR = 60 * MINUTE
PACIFIC = zoneinfo.ZoneInfo("America/Los_Angeles")
FORBIDDEN = ("sys.exit(print('gh: Forbidden (HTTP 403)', file=sys.stderr)"
             " or 1)")
RESET = ("sys.exit(print('Post \"https://api.github.com/\": read tcp:"
         " connection reset by peer', file=sys.stderr) or 1)")


def pacific(day, hour, minute=0):
    return int(datetime.datetime(2026, 11, day, hour, minute,
                                 tzinfo=PACIFIC).timestamp() * 1000)


class LimitFixture(ResultFixture):
    def seed(self, name, at, run, mode="act", settled=True):
        """A firing reserved at `at` for workflow run `run`; an act one
        canceled at once unless it is to stay `leased`."""
        conn = self.conn(name)
        project = conn.execute("SELECT id FROM projects").fetchone()[0]
        lease = None if mode == "dry_run" else int(time.time() * 1000) + HOUR
        firing = store.automations.reserve_firing(
            conn, project, "main_watch", 1, "main", f"seeded run {run}",
            mode=mode, reason="seeded", detail={
                "sha": S, "workflow_run_id": run, "jobs": [],
                "action": "rerun_failed_jobs", "run_attempt": 1},
            now=at, lease_until=lease)
        if mode == "act" and settled:
            store.automations.settle_firing(conn, firing, None, "canceled",
                                            "seeded", now=at)
        return firing

    def of_run(self, name, run):
        [firing] = [row for row in self.firings(name)
                    if row.detail["workflow_run_id"] == run
                    and row.reason != "seeded"]
        return firing

    def red_run(self, name, run):
        self.act_github.red(self.paths[name], self.repos[name], run)

    def worked_pass(self, at=None):
        """One pass at `at` whose workers really run; their exit codes."""
        children = []

        def spawn(argv, **kwargs):
            children.append(subprocess.Popen(argv, **kwargs))
            return children[-1]
        with patch.object(step, "SPAWN", spawn), \
                patch.object(step, "systemd_runner", lambda: None):
            sweep_pass(int(time.time() * 1000) if at is None else at)
        return [child.wait(timeout=120) for child in children]

    def refuse_posts(self, name, run, answer=FORBIDDEN):
        self.answer(name, f"EXEC BEFORE POST repos/o/{name}/actions/runs/{run}"
                    "/rerun-failed-jobs", answer)

    def open_pause(self, name):
        rows = self.conn(name).execute(
            "SELECT actor, note FROM automationPauses WHERE clearedAt IS NULL"
        ).fetchall()
        return rows[0] if rows else None


class RateTests(LimitFixture):
    def test_the_seventh_action_in_an_hour_is_refused(self):
        self.act("alpha")
        for index in range(6):
            self.seed("alpha", self.clock - (55 - 10 * index) * MINUTE,
                      100 + index)

        self.assertEqual(self.pass_at(self.clock), [])

        firing = self.of_run("alpha", 7)
        self.assertEqual((firing.mode, firing.state), ("act", "refused"))
        self.assertEqual(firing.reason, "rate: 6 actions in the last hour")

    def test_a_promoted_proposal_counts_from_its_promotion(self):
        self.act("alpha")
        for index in range(6):
            self.seed("alpha", self.clock - 2 * 24 * HOUR, 100 + index,
                      mode="dry_run")
            self.seed("alpha", self.clock - (55 - 10 * index) * MINUTE,
                      100 + index)

        self.assertEqual(self.pass_at(self.clock), [])

        firing = self.of_run("alpha", 7)
        self.assertEqual((firing.state, firing.reason),
                         ("refused", "rate: 6 actions in the last hour"))

    def test_the_day_count_resets_at_midnight_pacific(self):
        self.act("alpha")
        start, end = pacific(2, 1), pacific(2, 23)
        for index in range(30):
            self.seed("alpha", start + (end - start) * index // 29,
                      100 + index)

        self.assertEqual(self.pass_at(pacific(2, 23, 30)), [])
        refused = self.of_run("alpha", 7)
        self.assertEqual((refused.state, refused.reason),
                         ("refused", "rate: 30 actions today"))

        self.red_run("alpha", 8)
        self.assertEqual(len(self.pass_at(pacific(3, 0, 10))), 1)
        self.assertEqual(self.of_run("alpha", 8).state, "leased")

    def test_a_fourth_queued_firing_is_refused(self):
        self.act("alpha")
        for index in range(3):
            self.seed("alpha", self.clock - (30 - index) * MINUTE,
                      100 + index, settled=False)

        self.assertEqual(self.pass_at(self.clock), [])

        firing = self.of_run("alpha", 7)
        self.assertEqual((firing.state, firing.reason),
                         ("refused", "rate: 3 firings queued"))

    def test_proposals_are_not_limited(self):
        automations(self.paths["alpha"], "dry_run")
        self.red_run("alpha", 7)
        for index in range(6):
            self.seed("alpha", self.clock - (55 - 10 * index) * MINUTE,
                      100 + index, mode="dry_run")

        self.pass_at(self.clock)

        self.assertEqual(self.of_run("alpha", 7).state, "proposed")


class HostRerunCapTests(LimitFixture):
    def test_the_host_rerun_cap_counts_every_project(self):
        with open(self.home / HOST_FILE, "a") as registry:
            registry.write("\n[automations]\nreruns_per_day = 2\n")
        self.act("alpha")
        repo, path = "repos/o/alpha", self.paths["alpha"]
        self.act_github.answer(path, f"GET {repo}/commits/{S}/check-runs", {
            "total_count": 2, "check_runs": [
                job("unit", "failure", 11, 7), job("lint", "failure", 21, 8)]})
        self.act_github.answer(path, f"GET {repo}/rules/branches/main", [{
            "type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "unit"},
                                           {"context": "lint"}]}}])
        self.act_github.answer(path, f"GET {repo}/actions/runs/8",
                               {"id": 8, "run_attempt": 1})
        self.act("beta", run=9)

        self.pass_at(self.clock)
        self.pass_at(self.clock + MINUTE)

        fired = self.firings("alpha") + self.firings("beta")
        self.assertEqual(sorted(firing.state for firing in fired),
                         ["leased", "leased", "refused"])
        [refused] = [firing for firing in fired if firing.state == "refused"]
        self.assertEqual(refused.reason, "GitHub rerun cap: 2 today")
        self.assertEqual(self.state()["reruns"]["count"], 2)

    def test_a_lease_the_sweep_state_lost_still_counts_toward_the_cap(self):
        with open(self.home / HOST_FILE, "a") as registry:
            registry.write("\n[automations]\nreruns_per_day = 1\n")
        self.act("alpha")
        self.assertEqual(len(self.pass_at(self.clock)), 1)
        saved = self.state()
        del saved["reruns"]
        (self.home / "sweep.json").write_text(json.dumps(saved))

        self.red_run("alpha", 8)
        self.assertEqual(self.pass_at(self.clock + MINUTE), [])

        firing = self.of_run("alpha", 8)
        self.assertEqual((firing.state, firing.reason),
                         ("refused", "GitHub rerun cap: 1 today"))

    def test_a_skipped_projects_lease_still_counts_toward_the_cap(self):
        with open(self.home / HOST_FILE, "a") as registry:
            registry.write("\n[automations]\nreruns_per_day = 1\n")
        self.act("alpha")
        self.assertEqual(len(self.pass_at(self.clock)), 1)
        saved = self.state()
        del saved["reruns"]
        (self.home / "sweep.json").write_text(json.dumps(saved))
        store.set_admission(self.conn("alpha"), 1, "disabled", "retired")

        self.act("beta")
        self.assertEqual(self.pass_at(self.clock + MINUTE), [])

        firing = self.of_run("beta", 7)
        self.assertEqual((firing.state, firing.reason),
                         ("refused", "GitHub rerun cap: 1 today"))

    def test_a_wrong_rerun_cap_is_off_and_the_sweep_goes_on(self):
        with open(self.home / HOST_FILE, "a") as registry:
            registry.write("\n[automations]\nreruns_per_day = 0\n")
        self.act("alpha")
        silent = self.a_run("alpha")

        sweep_pass(T0 + 6 * MINUTE)
        printed = sweep_pass(T0 + 12 * MINUTE)

        self.assertEqual(self.conn("alpha").execute(
            "SELECT phase, outcome FROM runs WHERE id = ?",
            (silent,)).fetchone(), ("failed", "failed"))
        named = [line for line in printed.splitlines()
                 if "reruns_per_day" in line]
        self.assertEqual(len(named), 1, printed)
        self.assertEqual(self.firings("alpha"), [])


class SelfPauseTests(LimitFixture):
    def test_the_same_failure_twice_pauses_the_automation(self):
        self.act("alpha")
        start = self.clock - 4 * HOUR
        self.assertEqual(self.worked_pass(start), [0])
        self.rerun_answers("alpha", [job("unit", "failure", 12, 7)])
        self.pass_at(start + MINUTE)
        first = self.of_run("alpha", 7)
        self.assertEqual(first.detail["result"], "red_again")
        self.assertIsNone(self.open_pause("alpha"))

        self.red_run("alpha", 8)
        self.assertEqual(self.worked_pass(start + 3 * HOUR), [0])
        self.rerun_answers("alpha", [job("unit", "failure", 13, 8)])
        self.pass_at(start + 3 * HOUR + MINUTE)

        second = self.of_run("alpha", 8)
        self.assertEqual(second.detail["result"], "red_again")
        actor, note = self.open_pause("alpha")
        self.assertEqual(actor, "automation")
        self.assertIn(str(first.id), note)
        self.assertIn(str(second.id), note)
        self.red_run("alpha", 9)
        self.assertEqual(self.pass_at(start + 3 * HOUR + 2 * MINUTE), [])
        self.assertEqual(len(self.firings("alpha")), 2)
        out, _row = self.patrol("alpha")
        self.assertIn("automation main_watch paused", out)

        code, _err = self.holo("automation", "resume", "main_watch", "ok",
                               "-p", "alpha")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.pass_at(start + 3 * HOUR + 3 * MINUTE)), 1)
        self.assertEqual(self.of_run("alpha", 9).state, "leased")

    def test_two_github_refusals_in_a_row_pause_it(self):
        self.act("alpha")
        self.refuse_posts("alpha", 7)
        self.assertEqual(self.worked_pass(), [0])
        first = self.of_run("alpha", 7)
        self.assertEqual(first.state, "refused")
        self.assertIn("403", first.reason)
        self.assertIsNone(self.open_pause("alpha"))
        self.red_run("alpha", 8)
        self.refuse_posts("alpha", 8)
        self.assertEqual(self.worked_pass(), [0])

        second = self.of_run("alpha", 8)
        self.assertEqual(second.state, "refused")
        actor, note = self.open_pause("alpha")
        self.assertEqual(actor, "automation")
        self.assertIn(f"firings {first.id} and {second.id}", note)

        self.act("beta")
        self.refuse_posts("beta", 7)
        self.assertEqual(self.worked_pass(), [0])
        self.red_run("beta", 8)
        self.recorded_pass()
        self.answer("beta", "GET repos/o/beta/actions/runs/8",
                    {"id": 8, "run_attempt": 2})
        self.assertEqual(self.worker("beta", self.of_run("beta", 8).id)
                         .returncode, 0)
        self.assertIn("attempt 2", self.of_run("beta", 8).reason)
        self.red_run("beta", 9)
        self.refuse_posts("beta", 9)
        self.assertEqual(self.worked_pass(), [0])

        self.assertEqual([firing.state for firing in self.firings("beta")],
                         ["refused"] * 3)
        self.assertIn("403", self.of_run("beta", 9).reason)
        self.assertIsNone(self.open_pause("beta"))

    def test_a_limit_refusal_between_github_refusals_breaks_the_run(self):
        with open(self.home / HOST_FILE, "a") as registry:
            registry.write("\n[automations]\nreruns_per_day = 1\n")
        self.act("alpha")
        self.refuse_posts("alpha", 7)
        self.assertEqual(self.worked_pass(pacific(2, 10)), [0])
        self.red_run("alpha", 8)
        self.assertEqual(self.worked_pass(pacific(2, 10, 5)), [])
        self.assertEqual(self.of_run("alpha", 8).reason,
                         "GitHub rerun cap: 1 today")
        self.red_run("alpha", 9)
        self.refuse_posts("alpha", 9)

        self.assertEqual(self.worked_pass(pacific(3, 0, 10)), [0])

        self.assertIn("403", self.of_run("alpha", 9).reason)
        self.assertIsNone(self.open_pause("alpha"))

    def test_dispatches_github_never_answered_do_not_pause_it(self):
        self.act("alpha")
        for run in (7, 8):
            self.red_run("alpha", run)
            self.refuse_posts("alpha", run, RESET)
            self.assertEqual(self.worked_pass(), [0])
            self.assertIn("connection reset", self.of_run("alpha", run).reason)

        self.assertIsNone(self.open_pause("alpha"))


if __name__ == "__main__":
    import unittest
    unittest.main()
