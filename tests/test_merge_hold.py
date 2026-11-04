"""The main watch's merge hold: placed by the host sweep on a red main, and
read by `_merge_pr()` against a real `git` origin, with only GitHub's merge
call and check-run read patched.

Run: python3 -m unittest discover -s tests -p 'test_merge_hold.py' -v
"""
import io
import json
import subprocess
import time
from unittest.mock import patch

import holophyte.automation.merge_hold
import holophyte.pr.github
import holophyte.pr.merge_queue
import holophyte.pr.pr_status
import holophyte.pr.pullrequest
import store.automations
import store.operate
from holophyte.config.project import Project
from holophyte.host.registry import HOST_FILE
from holophyte.loop.gates import InfraFailure, MergeParked
from tests.main_watch_fixture import automations, job, sweep_pass
from tests.phase_fixture import advance_phase
from tests.test_automation_act import ActFixture, S
from tests.test_main_watch import S2
from tests.test_sweep_host import T0

URL = "https://github.com/o/alpha/pull/7"
MERGE_SHA = "9f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"


class HoldFixture(ActFixture):
    def holds(self, name="alpha"):
        return self.conn(name).execute(
            "SELECT sha, checks, firingId, endedAt, endReason FROM mergeHolds"
            " ORDER BY id").fetchall()

    def red_at(self, sha, name="alpha", failed=("unit",)):
        repo, path = self.repos[name], self.paths[name]
        self.act_github.answer(path, f"GET repos/{repo}/commits/main",
                               {"sha": sha})
        self.act_github.answer(path, f"GET repos/{repo}/rules/branches/main", [{
            "type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "unit"},
                                           {"context": "lint"}]}}])
        runs = [job(check, "failure" if check in failed else "success",
                    11 + index, 7)
                for index, check in enumerate(("unit", "lint"))]
        self.act_github.answer(
            path, f"GET repos/{repo}/commits/{sha}/check-runs",
            {"total_count": len(runs), "check_runs": runs})

    def open_hold(self):
        [(sha, checks)] = self.conn("alpha").execute(
            "SELECT sha, checks FROM mergeHolds WHERE endedAt IS NULL"
        ).fetchall()
        return sha, json.loads(checks)


class HoldPlacementTests(HoldFixture):
    def test_a_red_head_places_one_hold_and_a_newer_red_moves_it(self):
        self.act("alpha")

        self.recorded_pass()
        self.recorded_pass()

        [(sha, checks, firing_id, ended, _)] = self.holds()
        self.assertEqual((sha, json.loads(checks), ended), (S, ["unit"], None))
        firing = store.automations.firing(self.conn("alpha"), firing_id)
        self.assertEqual((firing.detail["action"], firing.mode, firing.state),
                         ("hold_merges", "act", "confirmed"))

        self.red_at(S2)
        self.recorded_pass()

        old, new = self.holds()
        self.assertEqual(old[0], S)
        self.assertIsNotNone(old[3])
        self.assertEqual(old[4], f"superseded by {S2[:12]}")
        self.assertEqual((new[0], json.loads(new[1]), new[3]),
                         (S2, ["unit"], None))

    def test_new_red_checks_at_the_same_head_replace_the_hold(self):
        self.act("alpha")
        self.red_at(S)
        self.recorded_pass()

        self.red_at(S, failed=("lint",))
        self.recorded_pass()

        self.assertEqual(self.open_hold(), (S, ["lint"]))
        self.assertEqual(len(self.holds()), 2)

    def test_a_red_head_main_returns_to_is_held_again(self):
        self.act("alpha")
        for sha in (S, S2, S):
            self.red_at(sha)
            self.recorded_pass()

        self.assertEqual(self.open_hold(), (S, ["unit"]))
        self.assertEqual([row[0] for row in self.holds()], [S, S2, S])

    def test_dry_run_only_proposes_the_hold(self):
        automations(self.paths["alpha"], "dry_run")
        self.act_github.red(self.paths["alpha"], self.repos["alpha"])

        sweep_pass(T0)

        self.assertEqual(self.holds(), [])
        [proposal] = [row for row in store.automations.firings(
            self.conn("alpha")) if row.subject == "merges"]
        self.assertEqual((proposal.mode, proposal.state),
                         ("dry_run", "proposed"))
        self.assertIn("would hold merges", proposal.reason)


class MergeGateFixture(HoldFixture):
    """`alpha`'s worktree clones a bare origin whose `main` holds `R`, made
    in another clone so the worktree lacks it until a fetch."""

    def setUp(self):
        super().setUp()
        self.origin = self.root / "origin.git"
        self.wt = self.root / "wt"
        other = self.root / "other"
        self.git(self.root, "init", "-q", "--bare", "-b", "main",
                 str(self.origin))
        self.git(self.root, "clone", "-q", str(self.origin), str(self.wt))
        self.git(self.wt, "checkout", "-qb", "main")
        self.commit(self.wt, "BASE.md")
        self.git(self.wt, "push", "-q", "origin", "main")
        self.git(self.root, "clone", "-q", str(self.origin), str(other))
        self.red_commit = self.commit(other, "RED.md")
        self.git(other, "push", "-q", "origin", "main")
        self.git(self.wt, "checkout", "-qb", "early")
        self.early = self.commit(self.wt, "EARLY.md")
        self.merged, self.runs_at = [], {}
        self.act("alpha")
        self.red_at(self.red_commit)
        self.recorded_pass()
        [(self.hold_sha, _, _, _, _)] = self.holds()
        config = Project.locate(self.paths["alpha"], adopt=False).config_path
        with config.open("a") as handle:
            handle.write('\n[merge]\nmode = "pr"\nrequire_up_to_date = false\n')

    def git(self, cwd, *args):
        return subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid",
             *args], cwd=cwd, check=True, capture_output=True,
            text=True).stdout.strip()

    def commit(self, cwd, path):
        (cwd / path).write_text(f"{path}\n")
        self.git(cwd, "add", path)
        self.git(cwd, "commit", "-qm", path)
        return self.git(cwd, "rev-parse", "HEAD")

    def fix(self):
        """A candidate branched from origin's `main`, so it carries `R`."""
        self.git(self.wt, "fetch", "-q", "origin", "main")
        self.git(self.wt, "checkout", "-qb", "fix", "FETCH_HEAD")
        return self.commit(self.wt, "FIX.md")

    def merge(self, sha, target=None):
        """`_merge_pr()` on `sha`; `target`, when given, the factory's own,
        whose config it read before this merge."""
        run = self.a_run("alpha")
        advance_phase(self.conn("alpha"), run, "merge_gate")
        target = target or Project.locate(self.paths["alpha"], adopt=False)
        pull = holophyte.pr.pr_status.parse_pr_url(URL)

        def merge_pull_request(_project, _pull, pinned):
            self.merged.append(pinned)
            return MERGE_SHA

        def check_runs_of(_target, _pull, ref):
            runs = self.runs_at.get(ref, [])
            return [{"id": index, "name": name,
                     "status": "in_progress" if result is None
                     else "completed", "conclusion": result}
                    for index, (name, result) in enumerate(runs, 1)]

        with patch.object(holophyte.pr.merge_queue, "merge_queue_required",
                          return_value=False), \
                patch.object(holophyte.pr.github, "merge_pull_request",
                             merge_pull_request), \
                patch.object(holophyte.pr.pr_status, "_check_runs_of",
                             check_runs_of), \
                patch("sys.stdout", io.StringIO()):
            try:
                holophyte.pr.pullrequest._merge_pr(
                    target, self.conn("alpha"), run, None, "KO-1", "early",
                    self.wt, sha, 1, pull)
            except MergeParked:
                return run, True
        return run, False

    def events(self, run, kind):
        return [json.loads(payload) for (payload,) in self.conn("alpha").execute(
            "SELECT payload FROM runEvents WHERE runId = ? AND kind = ?",
            (run, kind))]

    def hold_id(self):
        return self.conn("alpha").execute(
            "SELECT id FROM mergeHolds WHERE endedAt IS NULL").fetchone()[0]


class MergeGateTests(MergeGateFixture):
    def test_a_candidate_without_the_red_commit_is_parked(self):
        self.runs_at[self.early] = [("unit", "success")]

        run, parked = self.merge(self.early)

        self.assertTrue(parked)
        self.assertEqual(self.merged, [])
        self.assertEqual(self.hold_sha, self.red_commit)
        [(question,)] = self.conn("alpha").execute(
            "SELECT t.blockedQuestion FROM tickets AS t JOIN runs AS r"
            " ON r.ticketId = t.id WHERE r.id = ?",
            (run,)).fetchall()
        self.assertIn(self.red_commit[:12], question)
        self.assertIn("(unit)", question)
        self.assertEqual(self.events(run, "main_hold_parked"),
                         [{"hold": self.hold_id()}])

    def test_a_fix_that_carries_the_red_commit_and_passes_merges(self):
        fix = self.fix()
        self.runs_at[fix] = [("unit", "success")]

        run, parked = self.merge(fix)

        self.assertFalse(parked)
        self.assertEqual(self.merged, [fix])
        self.assertEqual(self.events(run, "main_hold_passed"),
                         [{"hold": self.hold_id()}])

    def test_a_full_hourly_limit_still_moves_the_hold_to_a_new_red_head(self):
        old_fix = self.fix()
        self.runs_at[old_fix] = [("unit", "success")]
        later = self.root / "later"
        self.git(self.root, "clone", "-q", str(self.origin), str(later))
        new_red = self.commit(later, "LATER.md")
        self.git(later, "push", "-q", "origin", "main")
        conn = self.conn("alpha")
        project = conn.execute("SELECT id FROM projects").fetchone()[0]
        now = int(time.time() * 1000)
        for run in range(100, 105):
            seeded = store.automations.reserve_firing(
                conn, project, "main_watch", 1, "main", f"seeded run {run}",
                mode="act", reason="seeded", detail={
                    "sha": S, "workflow_run_id": run, "jobs": [],
                    "action": "rerun_failed_jobs", "run_attempt": 1},
                now=now - 10 * 60 * 1000, lease_until=now + 60 * 60 * 1000)
            store.automations.settle_firing(conn, seeded, None, "canceled",
                                            "seeded", now=now)

        self.red_at(new_red, failed=("lint",))
        self.recorded_pass()

        [rerun] = [firing for firing in store.automations.firings(conn)
                   if firing.detail.get("sha") == new_red
                   and firing.subject == "main"]
        self.assertEqual((rerun.state, rerun.reason),
                         ("refused", "rate: 6 actions in the last hour"))
        self.assertEqual(self.open_hold(), (new_red, ["lint"]))
        self.assertTrue(self.merge(old_fix)[1])
        self.assertEqual(self.merged, [])

    def test_a_candidate_that_does_not_pass_the_held_checks_is_parked(self):
        fix = self.fix()
        self.runs_at[fix] = [("unit", "failure"), ("docs", "success")]
        failed_parked = self.merge(fix)[1]
        self.runs_at[fix] = [("docs", "success")]
        missing_parked = self.merge(fix)[1]

        self.assertEqual((failed_parked, missing_parked), (True, True))
        self.assertEqual(self.merged, [])

    def test_a_later_failed_or_pending_run_of_a_held_check_parks(self):
        fix = self.fix()
        self.runs_at[fix] = [("unit", "success"), ("unit", "failure")]
        failed_parked = self.merge(fix)[1]
        self.runs_at[fix] = [("unit", "success"), ("unit", None)]
        pending_parked = self.merge(fix)[1]

        self.assertEqual((failed_parked, pending_parked), (True, True))
        self.assertEqual(self.merged, [])

    def test_a_git_error_or_timeout_is_an_infra_failure_not_a_park(self):
        missing = "0" * 40
        with self.assertRaises(InfraFailure):
            holophyte.automation.merge_hold.carries(self.wt, missing,
                                                    self.early)
        timeout = subprocess.TimeoutExpired(["git"], 1)
        with patch.object(subprocess, "run", side_effect=timeout), \
                self.assertRaises(InfraFailure):
            holophyte.automation.merge_hold.carries(self.wt, self.red_commit,
                                                    self.early)
        self.assertFalse(holophyte.automation.merge_hold.carries(
            self.wt, self.red_commit, self.early))

    def test_held_runs_counts_only_runs_still_parked(self):
        conn = self.conn("alpha")
        run = self.merge(self.early)[0]
        self.assertEqual(store.automations.held_runs(conn, self.hold_id()), 1)

        store.operate.release(conn, run, "abandoned", "resumed")

        self.assertEqual(store.automations.held_runs(conn, self.hold_id()), 0)

    def test_an_unapproved_revision_or_report_only_repo_holds_nothing(self):
        config = Project.locate(self.paths["alpha"], adopt=False).config_path
        approved = config.read_text()
        config.write_text(approved.replace("revision = 1", "revision = 0"))
        self.assertFalse(self.merge(self.early)[1])
        config.write_text(approved)

        with open(self.home / HOST_FILE, "a") as registry:
            registry.write('\n[automations]\nreport_only = ["O/Alpha"]\n')
        self.assertFalse(self.merge(self.early)[1])

        self.assertEqual(self.merged, [self.early] * 2)
        self.assertIsNotNone(self.hold_id())

    def test_an_inhibited_or_dry_run_watch_holds_nothing(self):
        self.assertEqual(self.holo("automation", "pause", "main_watch", "x",
                                   "-p", "alpha")[0], 0)
        self.assertFalse(self.merge(self.early)[1])
        self.assertEqual(self.holo("automation", "resume", "main_watch", "y",
                                   "-p", "alpha")[0], 0)

        self.assertEqual(self.holo("automations", "off", "x")[0], 0)
        self.assertFalse(self.merge(self.early)[1])
        self.assertEqual(self.holo("automations", "on", "y")[0], 0)

        config = Project.locate(self.paths["alpha"], adopt=False).config_path
        config.write_text(config.read_text().replace('mode = "act"',
                                                     'mode = "dry_run"'))
        self.assertFalse(self.merge(self.early)[1])

        self.assertEqual(self.merged, [self.early] * 3)
        self.assertIsNotNone(self.hold_id())

    def test_the_gate_reads_the_watch_mode_as_it_stands_at_the_merge(self):
        target = Project.locate(self.paths["alpha"], adopt=False)
        config = target.config_path
        act = config.read_text()
        config.write_text(act.replace('mode = "act"', 'mode = "dry_run"'))
        self.assertEqual(target.config()["automations"]["main_watch"]["mode"],
                         "dry_run")
        config.write_text(act)
        self.assertTrue(self.merge(self.early, target)[1])

        target = Project.locate(self.paths["alpha"], adopt=False)
        self.assertEqual(target.config()["automations"]["main_watch"]["mode"],
                         "act")
        config.write_text(act.replace('mode = "act"', 'mode = "dry_run"'))
        self.assertFalse(self.merge(self.early, target)[1])

        self.assertEqual(self.merged, [self.early])
