"""`holo automation list`, `show ID` and `settle ID` over a real store, and
`holo report`'s `Automations` line.

Run: python3 -m unittest discover -s tests -p 'test_automation_audit.py' -v
"""
import json
import time

import store
import store.automations
import store.tickets
from holophyte.config.project import Project
from tests.host_fixture import HostFixture
from tests.test_holo_reads import holo

MIN = 60_000
HOUR = 60 * MIN
RED = "5e1f0c2a9b7d4e3f8a6c1b0d2e4f6a8c0b1d3e5f"
G = "0a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3"
PID = 4242
RERUN = {"action": "rerun_failed_jobs", "workflow_run_id": 7,
         "run_attempt": 1, "rerun_attempt": 2}
AUDITED = ("automationFirings", "mergeHolds", "interventions")


class AuditCase(HostFixture):
    def setUp(self):
        super().setUp()
        self.now = int(time.time() * 1000)
        self.path = self.repo("alpha")
        self.conn, self.project = self.store_of(self.path)

    def store_of(self, path):
        conn = store.open(str(Project.locate(path, adopt=False).store_path))
        self.addCleanup(conn.close)
        return conn, store.ensure_project(conn, "team-alpha", path)

    def fire(self, state, at, cause, detail=None, automation="main_watch",
             reason=None):
        """A firing created at `at` and left in `state` by the store's own
        writers: reserved, then for a settled state leased, taken and settled."""
        reason = reason or f"{state} at {cause}"
        if state == "proposed":
            return store.automations.reserve_firing(
                self.conn, self.project, automation, 1, "main", cause,
                mode="dry_run", reason=reason, detail=detail or {}, now=at)
        firing = store.automations.reserve_firing(
            self.conn, self.project, automation, 1, "main", cause, mode="act",
            reason=reason, detail=detail or {}, now=at, lease_until=at + MIN)
        store.automations.take_firing(self.conn, firing, PID, now=at)
        store.automations.settle_firing(self.conn, firing, PID, state, reason,
                                        now=at, detail=detail)
        return firing

    def run_of(self, key, at):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project, linear_issue_id=f"issue-{key}",
            linear_identifier=key, title=f"ticket {key}")
        return store.claim(self.conn, self.project, ticket, now=at)

    def released(self, run, firing, at):
        return store.record_intervention(
            self.conn, run, "babysit", "main is green; sent back",
            source="automation", now=at, firing_id=firing)

    def holo(self, *argv):
        return holo(*argv, "-p", str(self.path))

    def seed_25(self):
        """25 firings a minute apart, oldest first, among them a proposal, a
        confirmed rerun at rerun_attempt 2 and a refusal."""
        states = ["proposed", "confirmed", "refused"] + ["canceled"] * 22
        ids = []
        for n, state in enumerate(states):
            detail = RERUN if state == "confirmed" else {}
            ids.append(self.fire(state, self.now - (30 - n) * MIN,
                                 f"o/alpha@{n:040x} run {n}", detail))
        return ids

    def audit(self):
        return {table: self.conn.execute(
            f"SELECT COUNT(*), MAX(id) FROM {table}").fetchone()
            for table in AUDITED}


class ListTests(AuditCase):
    def test_list_prints_the_newest_firings_first(self):
        ids = self.seed_25()

        code, out, err = self.holo("automation", "list")

        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 20)
        newest = list(reversed(ids))[:20]
        for line, firing in zip(lines, newest):
            self.assertTrue(line.startswith(f"{firing} main_watch "), line)
        code, out, err = self.holo("automation", "list", "--limit", "30",
                                   "--json")
        self.assertEqual(code, 0, err)
        firings = json.loads(out)["firings"]
        self.assertEqual([row["id"] for row in firings], list(reversed(ids)))
        [rerun] = [row for row in firings if row["state"] == "confirmed"]
        self.assertEqual(rerun["detail"]["rerun_attempt"], 2)
        columns = len(self.conn.execute(
            "PRAGMA table_info(automationFirings)").fetchall())
        self.assertEqual({len(row) for row in firings}, {columns})


class ShowCase(AuditCase):
    def seed_release(self):
        """Hold H placed at a red sha, then a `release_held_merges` firing
        that ended H at green sha G and sent KO-11 and KO-22 back."""
        at = self.now - HOUR
        holder = store.automations.reserve_firing(
            self.conn, self.project, "main_watch", 1, "merges", f"o/alpha@{RED}",
            mode="act", reason="holding merges",
            detail={"action": "hold_merges"}, now=at, lease_until=at + MIN)
        self.hold = store.automations.place_merge_hold(
            self.conn, self.project, holder, RED, ["unit"], "held merges",
            now=at)
        runs = {key: self.run_of(key, at) for key in ("KO-11", "KO-22")}
        later = self.now - MIN
        detail = {"action": "release_held_merges", "sha": G, "hold": self.hold}
        firing = store.automations.reserve_firing(
            self.conn, self.project, "main_watch", 1, "merges",
            f"o/alpha@{G} hold {self.hold}", mode="act",
            reason="releasing the merge hold", detail=detail, now=later,
            lease_until=later + MIN)
        store.automations.end_merge_hold(
            self.conn, self.hold, f"main green at {G[:12]}", now=later)
        for run in runs.values():
            self.released(run, firing, later)
        store.automations.settle_firing(
            self.conn, firing, None, "confirmed", "released the merge hold",
            now=later, detail=detail | {"released": list(runs), "left": []})
        return firing


class ShowTests(ShowCase):
    def test_show_prints_a_firing_with_its_hold_and_interventions(self):
        firing = self.seed_release()

        code, out, err = self.holo("automation", "show", str(firing))

        self.assertEqual(code, 0, err)
        key = self.conn.execute(
            "SELECT firingKey FROM automationFirings WHERE id = ?",
            (firing,)).fetchone()[0]
        self.assertIn(key, out)
        self.assertIn("state     confirmed", out)
        self.assertIn(G[:12], out)
        [hold] = [line for line in out.splitlines()
                  if line.startswith(f"hold {self.hold} ")]
        self.assertIn(f"main green at {G[:12]}", hold)
        babysits = [line for line in out.splitlines() if " babysit " in line]
        self.assertEqual(len(babysits), 2, out)
        self.assertEqual([("KO-11" in line, "KO-22" in line)
                          for line in babysits], [(True, False), (False, True)])

    def test_show_refuses_an_unknown_id(self):
        code, out, err = self.holo("automation", "show", "999")

        self.assertEqual(code, 1)
        self.assertIn("999", f"{out}{err}")

    def test_list_and_show_write_nothing(self):
        firing = self.seed_release()
        self.seed_25()
        before = self.audit()

        for argv in (["list"], ["list", "--json"], ["show", str(firing)],
                     ["show", str(firing), "--json"]):
            code, _, err = self.holo("automation", *argv)
            self.assertEqual(code, 0, err)

        self.assertEqual(self.audit(), before)


class SettleTests(AuditCase):
    def row(self, firing):
        return self.conn.execute(
            "SELECT state, detail, settledAt FROM automationFirings"
            " WHERE id = ?", (firing,)).fetchone()

    def test_settle_closes_only_an_unknown_firing(self):
        firing = self.fire("unknown", self.now - HOUR, "o/alpha@x run 1")

        code, _, err = self.holo("automation", "settle", str(firing),
                                 "confirmed", "rerun is on GitHub")

        self.assertEqual(code, 0, err)
        state, detail, _ = self.row(firing)
        self.assertEqual(state, "confirmed")
        settled_by = json.loads(detail)["settled_by"]
        self.assertEqual(settled_by["note"], "rerun is on GitHub")
        self.assertTrue(settled_by["login"])
        settled = self.row(firing)

        code, out, err = self.holo("automation", "settle", str(firing),
                                   "canceled", "again")

        self.assertEqual(code, 1)
        self.assertIn("confirmed", f"{out}{err}")
        self.assertEqual(self.row(firing), settled)


class ReportTests(AuditCase):
    def test_report_counts_the_windows_firings(self):
        empty = self.repo("beta")
        self.store_of(empty)
        code, out, err = holo("report", "--since", "24h", "-p", str(empty))
        self.assertEqual(code, 0, err)
        self.assertNotIn("Automations", out)
        states = (["proposed"] * 2 + ["confirmed"] * 2 + ["refused", "canceled",
                                                         "unknown"])
        for n, state in enumerate(states):
            self.fire(state, self.now - (n + 1) * HOUR, f"o/alpha@{n} run {n}")
        self.fire("confirmed", self.now - 2 * 24 * HOUR, "o/alpha@old run 9")
        at = self.now - 10 * HOUR
        holder = store.automations.reserve_firing(
            self.conn, self.project, "main_watch", 1, "merges", f"o/alpha@{RED}",
            mode="act", reason="holding merges", detail={}, now=at,
            lease_until=at + MIN)
        store.automations.place_merge_hold(self.conn, self.project, holder,
                                           RED, ["unit"], "held", now=at)
        for key in ("KO-11", "KO-22"):
            self.released(self.run_of(key, at), holder, at)

        code, out, err = self.holo("report", "--since", "24h")

        self.assertEqual(code, 0, err)
        [line] = [line for line in out.splitlines()
                  if line.startswith("Automations")]
        self.assertEqual(
            line.removeprefix("Automations").strip(),
            "8 firings (proposed 2 · confirmed 3 · refused 1 · canceled 1 ·"
            " unknown 1) · 1 hold · 2 runs released")
