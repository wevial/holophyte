"""`holo report` over a real store: the window's counts first, its runs
next, the consumed send-back notes only on `--notes`, and a `--json` object
holding all of them.

Run: python3 -m unittest discover -s tests -p 'test_holo_report.py' -v
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.tickets
from holophyte.config.project import Project
from store.operator_notes import consume, send_back
from tests.phase_fixture import finish_run, park_run
from tests.test_holo_reads import holo

MIN = 60 * 1000
DAY = 24 * 60 * MIN
FIRST_NOTE = "split the parser before the renderer"
SECOND_NOTE = "name the window in the header"
COUNT_LABELS = ("Shipped", "Failures", "Gaps", "Hands-on")


class ReportCase(unittest.TestCase):
    """A store whose runs end relative to the wall clock, as the command's
    window is."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        environ = {key: value for key, value in os.environ.items()
                   if key not in ("HOLO_PROJECT", "NO_COLOR")}
        environ["HOLOPHYTE_HOME"] = str(root / "home")
        self.enterContext(patch.dict(os.environ, environ, clear=True))
        self.target = root / "repo"
        self.target.mkdir()
        self.project = Project.locate(self.target, adopt=False)
        self.project.holo_dir.mkdir(parents=True)
        self.now = int(time.time() * 1000)
        self.conn = store.open(str(self.project.store_path))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(self.conn, "team-1",
                                                       self.target)

    def run_ended(self, identifier, outcome, actual, estimate, ago, kind=None):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{identifier}",
            linear_identifier=identifier, title=f"ticket {identifier}",
            time_box_ms=estimate * MIN)
        ended = self.now - ago
        run = store.claim(self.conn, self.project_id, ticket,
                          now=ended - actual * MIN - MIN)
        self.conn.execute("UPDATE runs SET workingMs = ? WHERE id = ?",
                          (actual * MIN, run))
        self.conn.commit()
        finish_run(self.conn, run, outcome, now=ended, failure_kind=kind)
        return run

    def holo(self, *args):
        return holo("report", *args, "-p", str(self.target))

    def read(self, *args):
        code, out, err = self.holo(*args)
        self.assertEqual(code, 0, err)
        return out


class WindowReportCase(ReportCase):
    """Inside the last seven days: three merged runs of 12, 16 and 30
    minutes against 30, 30 and 45, one abandoned, one failed at verify and
    one at the review route; and one merged run ended eight days ago."""

    def setUp(self):
        super().setUp()
        self.window_runs = [
            self.run_ended("KO-1", "merged", 12, 30, 6 * DAY),
            self.run_ended("KO-2", "merged", 16, 30, 3 * DAY),
            self.run_ended("KO-3", "merged", 30, 45, DAY),
            self.run_ended("KO-4", "abandoned", 5, 30, 2 * DAY),
            self.run_ended("KO-5", "failed", 20, 30, 60 * MIN, "verify"),
            self.run_ended("KO-6", "failed", 25, 30, 30 * MIN, "review_route"),
        ]
        self.old_run = self.run_ended("KO-9", "merged", 40, 30, 8 * DAY)

    def send_back_both_notes(self):
        run = self.window_runs[2]
        for note in (FIRST_NOTE, SECOND_NOTE):
            store.record_event(
                self.conn, run, "operator_note", f"maintainer: {note}",
                level="detail", now=self.now - 2 * MIN,
                payload=json.dumps({"note": note, "author": "maintainer"}))
            (event,) = self.conn.execute(
                "SELECT MAX(id) FROM runEvents WHERE kind = 'operator_note'"
            ).fetchone()
            consume(self.conn, run, [event], 1)


class CountTests(WindowReportCase):
    def test_json_counts_only_the_runs_ended_in_the_last_seven_days(self):
        body = json.loads(self.read("--json"))
        self.assertEqual(body["shipped"], {
            "merged": 3, "abandoned": 1, "failed": 2,
            "median_min": 16, "median_estimate_min": 30})
        self.assertEqual(body["failures"], {"verify": 1, "review": 1})
        self.assertNotIn(self.old_run, [row["run"] for row in body["runs"]])

    def test_since_all_counts_the_eight_day_old_merge(self):
        body = json.loads(self.read("--since", "all", "--json"))
        self.assertEqual(body["shipped"]["merged"], 4)
        self.assertIn(self.old_run, [row["run"] for row in body["runs"]])

    def test_hands_on_counts_the_windows_human_interventions_by_action(self):
        run = self.window_runs[0]
        for action, ago in (("requeue", DAY), ("requeue", 2 * DAY),
                            ("approve", DAY), ("requeue", 8 * DAY)):
            store.record_intervention(self.conn, run, action, "by hand",
                                      now=self.now - ago)
        body = json.loads(self.read("--json"))
        self.assertEqual(body["hands_on"]["by_action"],
                         {"requeue": 2, "approve": 1})
        hands_on = next(line for line in self.read().splitlines()
                        if line.startswith("Hands-on"))
        self.assertIn("3 interventions (requeue 2 · approve 1)", hands_on)

    def test_a_send_back_counts_once_as_a_send_back_not_an_intervention(self):
        parked = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id="issue-KO-8",
            linear_identifier="KO-8", title="parked ticket",
            acceptance_criteria=["Given KO-8, then it is worked"],
            verification_commands=["echo ok"])
        store.tickets.transition(self.conn, parked, "in_flight")
        run = store.claim(self.conn, self.project_id, parked, now=self.now - DAY)
        park_run(self.conn, run, "awaiting_merge_approval",
                 pr_url="https://example.test/org/repo/pull/8")
        store.tickets.transition(self.conn, parked, "blocked_on_operator")
        send_back(self.conn, run, FIRST_NOTE, "maintainer")
        body = json.loads(self.read("--json"))
        self.assertEqual(body["hands_on"], {"interventions": 0, "by_action": {},
                                            "send_backs": 1})
        self.assertEqual(body["shipped"]["merged"], 3)


    def test_estimate_median_pairs_only_the_merges_with_measured_work(self):
        for identifier in ("KO-10", "KO-11"):
            unmeasured = self.run_ended(identifier, "merged", 1, 100, DAY)
            self.conn.execute("UPDATE runs SET workingMs = NULL WHERE id = ?",
                              (unmeasured,))
            self.conn.commit()
        shipped = json.loads(self.read("--json"))["shipped"]
        self.assertEqual((shipped["merged"], shipped["median_min"],
                          shipped["median_estimate_min"]), (5, 16, 30))


class NoteTests(WindowReportCase):
    def setUp(self):
        super().setUp()
        self.send_back_both_notes()

    def test_page_opens_with_the_counts_and_shows_no_note_text(self):
        out = self.read()
        self.assertNotIn(FIRST_NOTE, out)
        self.assertNotIn(SECOND_NOTE, out)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual([line.split()[0] for line in lines[:4]],
                         list(COUNT_LABELS))
        self.assertEqual(lines[4], "Runs (6)")
        self.assertEqual(lines[-1], "repo · last 7 days")

    def test_notes_flag_lists_both_notes_after_the_counts_newest_first(self):
        lines = self.read("--notes").splitlines()
        hands_on = next(index for index, line in enumerate(lines)
                        if line.startswith("Hands-on"))
        second, first = (next(index for index, line in enumerate(lines)
                              if note in line)
                         for note in (SECOND_NOTE, FIRST_NOTE))
        self.assertLess(hands_on, second)
        self.assertLess(second, first)

    def test_json_without_notes_flag_holds_the_notes_and_every_window_run(self):
        body = json.loads(self.read("--json"))
        self.assertEqual([note["note"] for note in body["notes"]],
                         [SECOND_NOTE, FIRST_NOTE])
        self.assertEqual(sorted(row["run"] for row in body["runs"]),
                         sorted(self.window_runs))


class EdgeTests(ReportCase):
    def test_an_unknown_window_exits_2_naming_the_accepted_forms(self):
        code, out, err = self.holo("--since", "7x")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Nh, Nd or all", err)
        self.assertIn("'7x'", err)

    def test_a_window_reaching_before_the_epoch_counts_from_the_epoch(self):
        run = self.run_ended("KO-1", "failed", 5, 30, DAY, "verify")
        body = json.loads(self.read("--since", "100000000000000d", "--json"))
        self.assertEqual(body["window"]["from_ms"], 0)
        self.assertEqual(body["failures"], {"verify": 1})
        self.assertEqual([row["run"] for row in body["runs"]], [run])

    def test_a_store_with_no_ended_runs_says_nothing_shipped(self):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id="issue-KO-7",
            linear_identifier="KO-7", title="live ticket")
        store.claim(self.conn, self.project_id, ticket, now=self.now - MIN)
        out = self.read()
        self.assertIn("Shipped   nothing shipped in the last 7 days", out)
        self.assertNotIn("Runs", out)


if __name__ == "__main__":
    unittest.main()
