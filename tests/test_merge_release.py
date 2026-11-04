"""The main watch's release of its merge hold on a green head: the host
sweep ends the hold and sends each run it parked back to the babysitter,
and a released run then takes the loop's normal path on its pull request.

Run: python3 -m unittest discover -s tests -p 'test_merge_release.py' -v
"""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

import store
import store.automations
import store.schema
from holophyte.automation import main_watch
from holophyte.automation.automation_config import main_watch_config
from holophyte.pr import github
from tests.main_watch_fixture import job
from tests.test_merge_hold import URL, MergeGateFixture

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    Commit,
    Idle,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    MergeModeFixture,
)

G = "0a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3"


class ReleaseFixture(MergeGateFixture):
    def head_at(self, sha, results):
        """Main's head at `sha`, each required check's newest run answering
        its conclusion, or still in progress for None."""
        repo, path = self.repos["alpha"], self.paths["alpha"]
        self.act_github.answer(path, f"GET repos/{repo}/commits/main",
                               {"sha": sha})
        runs = [job(name, result, 21 + index, 9,
                    status="in_progress" if result is None else "completed")
                for index, (name, result) in enumerate(results.items())]
        self.act_github.answer(
            path, f"GET repos/{repo}/commits/{sha}/check-runs",
            {"total_count": len(runs), "check_runs": runs})

    def green(self):
        self.head_at(G, {"unit": "success", "lint": "success"})

    def parked(self):
        run, parked = self.merge(self.early)
        self.assertTrue(parked)
        return run, self.ticket_of(run)

    def ticket_of(self, run):
        return self.conn("alpha").execute(
            "SELECT ticketId FROM runs WHERE id = ?", (run,)).fetchone()[0]

    def identifier(self, ticket):
        return self.conn("alpha").execute(
            "SELECT linearIdentifier FROM tickets WHERE id = ?",
            (ticket,)).fetchone()[0]

    def interventions(self, run):
        return self.conn("alpha").execute(
            'SELECT "action", source, actor, firingId FROM interventions'
            " WHERE runId = ? ORDER BY id", (run,)).fetchall()

    def released(self):
        return self.conn("alpha").execute(
            "SELECT COUNT(*) FROM interventions WHERE source = 'automation'"
        ).fetchone()[0]

    def release_firing(self):
        [firing] = [row for row in store.automations.firings(
            self.conn("alpha"))
            if row.detail.get("action") == "release_held_merges"]
        return firing


class ReleaseTests(ReleaseFixture):
    def test_a_green_head_ends_the_hold_and_wakes_its_runs(self):
        hold = self.hold_id()
        parked = [self.parked(), self.parked()]
        self.green()

        self.recorded_pass()

        [(ended, reason)] = self.conn("alpha").execute(
            "SELECT endedAt, endReason FROM mergeHolds WHERE id = ?",
            (hold,)).fetchall()
        self.assertIsNotNone(ended)
        self.assertEqual(reason, f"main green at {G[:12]}")
        firing = self.release_firing()
        self.assertEqual((firing.mode, firing.state), ("act", "confirmed"))
        for run, ticket in parked:
            self.assertEqual(self.conn("alpha").execute(
                "SELECT t.status, r.outcome, r.resumePhase, r.prUrl"
                " FROM tickets AS t JOIN runs AS r ON r.id = t.lastRunId"
                " WHERE t.id = ?", (ticket,)).fetchall(),
                [("ready", "abandoned", "merge_gate", URL)])
            self.assertEqual(self.interventions(run),
                             [("babysit", "automation", "supervisor",
                               firing.id)])
        self.assertEqual(firing.detail["released"],
                         [self.identifier(ticket) for _, ticket in parked])
        self.assertEqual(firing.detail["left"], [])

    def test_a_ticket_a_person_touched_is_left_alone(self):
        run_c, ticket_c = self.parked()
        run_d, ticket_d = self.parked()
        self.assertEqual(self.holo("babysit", self.identifier(ticket_c),
                                   "looking", "-p", "alpha")[0], 0)
        self.green()

        self.recorded_pass()

        self.assertEqual([row[1:] for row in self.interventions(run_c)],
                         [("human", "operator", None)])
        self.assertEqual([row[1] for row in self.interventions(run_d)],
                         ["automation"])
        firing = self.release_firing()
        self.assertEqual(firing.detail["released"],
                         [self.identifier(ticket_d)])
        self.assertEqual(firing.detail["left"], [
            {"ticket": self.identifier(ticket_c),
             "reason": f"a person acted on run {run_c} after the hold"
                       " parked it"}])

    def test_a_pending_or_red_head_keeps_the_hold(self):
        hold = self.hold_id()
        self.parked()

        self.head_at(self.red_commit, {"unit": None, "lint": "success"})
        self.recorded_pass()
        self.assertEqual(self.hold_id(), hold)
        self.head_at(self.red_commit, {"unit": "failure", "lint": "success"})
        self.recorded_pass()

        self.assertEqual(self.hold_id(), hold)
        self.assertEqual(self.released(), 0)

    def test_a_paused_watch_releases_on_the_pass_after_resume(self):
        self.parked()
        self.green()
        self.assertEqual(self.holo("automation", "pause", "main_watch", "x",
                                   "-p", "alpha")[0], 0)

        self.recorded_pass()
        self.assertIsNotNone(self.hold_id())
        self.assertEqual(self.released(), 0)

        self.assertEqual(self.holo("automation", "resume", "main_watch", "ok",
                                   "-p", "alpha")[0], 0)
        self.recorded_pass()

        self.assertIsNone(self.conn("alpha").execute(
            "SELECT id FROM mergeHolds WHERE endedAt IS NULL").fetchone())
        self.assertEqual(self.released(), 1)

    def test_automation_rows_are_not_toil(self):
        self.parked()
        self.parked()
        self.green()
        self.recorded_pass()
        self.assertEqual(self.released(), 2)

        code, _, out = self.holo_out("report", "-p", "alpha")

        self.assertEqual(code, 0)
        [toil] = [line for line in out.splitlines()
                  if line.startswith("Hands-on")]
        self.assertTrue(toil.endswith(" · 0 send-backs"), toil)
        self.assertNotIn("babysit", toil)


class ReleasedRunLoopTests(MergeModeFixture):
    """A run parked on the hold by the real merge gate, released by the main
    watch, then claimed by the loop again."""

    CONFIG = ('[merge]\nmode = "pr"\napprove = "{approve}"\n\n'
              '[automations.main_watch]\nmode = "act"\nrevision = 1\n')

    def released_run(self):
        self.configure(self.CONFIG.format(approve="auto"))
        red = self.git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m",
                       "red").strip()
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        project = store.tickets.ensure_project(
            conn, self.provider().team, self.target)
        now = int(time.time() * 1000)
        firing = store.automations.reserve_firing(
            conn, project, "main_watch", 1, "merges", f"example/repo@{red}",
            mode="act", reason="r", detail={"action": "hold_merges"},
            now=now, lease_until=now + 60_000)
        store.automations.place_merge_hold(conn, project, firing, red,
                                           ["unit"], "held", now=now)
        self.fake_route()
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  provider=self.provider())
        self.assertEqual(self.read("SELECT phase FROM runs"),
                         [("awaiting_merge_approval",)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM runEvents"
                                   " WHERE kind = 'main_hold_parked'"),
                         [(1,)])
        self.assertEqual(self.merges(), 0)
        self.assertIsNotNone(main_watch.release_held_merges(
            conn, project, github._origin_pull(self.project), G, now,
            main_watch_config(self.project), frozenset()))
        self.assertEqual(self.read("SELECT status FROM tickets"), [("ready",)])

    def merges(self):
        return sum(kind == "merge" for kind, _ in self.api_calls())

    def test_a_released_run_merges_under_auto(self):
        self.released_run()

        self.loop(Idle(""), provider=self.provider())

        self.assertEqual(self.merges(), 1)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("abandoned",), ("merged",)])

    def test_a_released_run_parks_under_human(self):
        self.released_run()
        self.configure(self.CONFIG.format(approve="human"))

        self.loop(Idle(""), provider=self.provider())

        self.assertEqual(self.merges(), 0)
        self.assertEqual(self.read("SELECT phase FROM runs ORDER BY id"),
                         [("failed",), ("awaiting_merge_approval",)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("blocked_on_operator",)])


class WidenedInterventionsTests(unittest.TestCase):
    def test_the_widened_interventions_table_keeps_its_history(self):
        main_ddl = store.schema._INTERVENTIONS_DDL.replace(
            ", 'automation'", "").replace(
            "    firingId  INTEGER REFERENCES automationFirings (id),\n", "")
        self.assertNotIn("automation", main_ddl.partition("source")[2]
                         .partition("trigger")[0])
        self.assertNotIn("firingId", main_ddl)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = store.open(path)
            project = store.ensure_project(conn, "team-1", "/repo")
            ticket = store.mirror_ticket(
                conn, project, linear_issue_id="issue-1",
                linear_identifier="KO-1", title="candidate")
            run = store.claim(conn, project, ticket, now=1)
            conn.execute("DROP TABLE interventions")
            conn.execute(main_ddl)
            rows = [(run, None, "human", "manual", "requeue", "again", 10),
                    (run, None, "supervisor", "time_box", "kill", "late", 20),
                    (None, project, "factory", "manual", "hold", "held", 30)]
            conn.executemany(
                'INSERT INTO interventions (runId, projectId, source,'
                ' "trigger", "action", note, at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                rows)
            conn.execute("PRAGMA user_version = 66")
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    'INSERT INTO interventions (runId, source, "trigger",'
                    " \"action\", note, at) VALUES (?, 'automation', 'manual',"
                    " 'babysit', 'n', 40)", (run,))
            conn.close()

            conn = store.open(path)
            self.addCleanup(conn.close)

            self.assertEqual(conn.execute(
                'SELECT runId, projectId, source, "trigger", "action", note,'
                " at FROM interventions WHERE action != 'migrate'"
                " ORDER BY id").fetchall(), rows)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM interventions WHERE firingId IS NOT NULL"
            ).fetchone(), (0,))
            firing = store.automations.reserve_firing(
                conn, project, "main_watch", 1, "merges", "cause", mode="act",
                reason="r", detail={}, now=50, lease_until=60)
            added = store.record_intervention(
                conn, run, "babysit", "released", source="automation",
                firing_id=firing)
            self.assertEqual(conn.execute(
                "SELECT source, actor, firingId FROM interventions"
                " WHERE id = ?", (added,)).fetchone(),
                ("automation", "supervisor", firing))
            self.assertEqual(json.loads(store.schema.latest_migration_note(
                conn))["from"], 66)


if __name__ == "__main__":
    unittest.main()
