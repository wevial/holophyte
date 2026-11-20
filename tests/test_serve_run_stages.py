"""`GET /runs/N/stages`: run N's pinned pipeline, its stage attempts in start
order, and its ticket's other runs since the last merge as lanes, read from a
real store through the daemon.

Run: python3 -m unittest discover -s tests -p 'test_serve_run_stages.py' -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serve_fixture import MIN, ServeTestCase  # noqa: E402 - after the insert

import holophyte.config.project  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.pipelines  # noqa: E402 - after the sys.path insert above
import store.stages  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from holophyte.stages import record, standard  # noqa: E402
from tests.phase_fixture import finish_run, park_run  # noqa: E402
from tests.serve_contract_fixture import normalize_contract  # noqa: E402

T0 = 1_750_000_000_000
SHA = {name: name[0] * 40 for name in ("base", "candidate", "fixed", "e")}
CONFIG = """\
[agents]
review_model = "gpt-review-under-test"
review_effort = "high"

[agents.implementer]
harness = "claude"
model = "claude-model-under-test"
"""
SECRET = 'stage-secret-"value"-0123456789'
FIXTURE = Path(__file__).parent / "fixtures" / "serve" / "run-stages.json"


class RunStagesTests(ServeTestCase):
    maxDiff = None

    def setUp(self):
        super().setUp()
        (self.db.parent / "config.toml").write_text(CONFIG)
        self.project = holophyte.config.project.Project.locate(self.target)
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(self.conn, "team-1",
                                                       self.target)

    def ticket(self, key):
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{key}",
            linear_identifier=key, title=f"ticket {key}",
            acceptance_criteria=[f"Given {key}, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=60 * MIN)

    def claim(self, ticket, now, document=None):
        (status,) = self.conn.execute("SELECT status FROM tickets WHERE id = ?",
                                      (ticket,)).fetchone()
        if status != "in_flight":
            store.tickets.transition(self.conn, ticket, "in_flight")
        run = store.claim(self.conn, self.project_id, ticket, now=now)
        if document is not None:
            store.pipelines.pin_pipeline(self.conn, run, standard.NAME,
                                         document, now=now)
        return run

    def attempt(self, run, stage, opened, closed=None, sha=SHA["base"],
                detail=None, roles=()):
        attempt = store.stages.open_stage(self.conn, run, stage, sha, detail,
                                          now=opened)
        if closed is not None:
            outcome, edge, at = closed
            store.stages.close_stage(self.conn, attempt, outcome, edge,
                                     SHA["candidate"], roles, now=at)
        return attempt

    def seed_three_runs(self):
        """Run 1 merged, run 2 failed under revision 1, run 3 live under
        revision 2 with a verify attempt opened late but started early."""
        first = standard.resolve(self.project)
        second = standard.resolve(self.project, "sonnet")
        ticket = self.ticket("KO-40")
        merged = self.claim(ticket, T0, first)
        self.attempt(merged, "implement", T0 + MIN, ("pass", "next", T0 + 2 * MIN))
        finish_run(self.conn, merged, "merged", now=T0 + 10 * MIN)
        failed = self.claim(ticket, T0 + 20 * MIN, first)
        self.attempt(failed, "implement", T0 + 21 * MIN,
                     ("pass", "next", T0 + 22 * MIN), detail={"kind": "first"})
        self.attempt(failed, "verify", T0 + 23 * MIN,
                     ("fail", "end", T0 + 24 * MIN), detail={"round": 1})
        finish_run(self.conn, failed, "failed", now=T0 + 30 * MIN)
        live = self.claim(ticket, T0 + 40 * MIN, second)
        implement = store.stages.open_stage(
            self.conn, live, "implement", SHA["base"], {"kind": "first"},
            now=T0 + 41 * MIN)
        store.record_event(
            self.conn, live, "agent_turn", "implement turn ended",
            level="detail", payload=json.dumps(dict(
                role="implement", seconds=50.5, usage=dict(
                    input_tokens=1200, output_tokens=300, cost_usd=0.25))))
        store.stages.close_stage(self.conn, implement, "pass", "next",
                                 SHA["candidate"], ["implement"],
                                 now=T0 + 42 * MIN)
        store.stages.skip_stage(self.conn, live, "trim",
                                "[trim] enabled = false", SHA["candidate"],
                                now=T0 + 42 * MIN)
        self.attempt(live, "review", T0 + 45 * MIN, sha=SHA["candidate"],
                     detail={"round": 1, "adversary": "off"})
        self.attempt(live, "verify", T0 + 43 * MIN,
                     ("pass", "next", T0 + 44 * MIN), sha=SHA["candidate"],
                     detail={"round": 1})
        self.conn.commit()
        return merged, failed, live

    def stages(self, run):
        code, _, body = self.request("GET", f"/runs/{run}/stages")
        self.assertEqual(code, 200)
        return body

    def test_a_live_run_serves_its_revision_its_attempts_and_its_failed_lane(self):
        _, failed, live = self.seed_three_runs()
        self.start()
        body = self.stages(live)

        pipeline = body["pipeline"]
        self.assertEqual((pipeline["name"], pipeline["revision"],
                          pipeline["format"]), ("Holophyte standard", 2, 1))
        self.assertEqual([(stage["id"], stage["type"])
                          for stage in pipeline["stages"]],
                         [("critic", "check"), ("implement", "build"),
                          ("trim", "build"), ("verify", "check"),
                          ("review", "check"), ("merge_gate", "check"),
                          ("waiting", "check")])
        self.assertEqual(pipeline["stages"][1]["profile"], "sonnet")
        self.assertEqual(body["attempts"], [
            {"stage": "implement", "attempt": 1, "started_ms": T0 + 41 * MIN,
             "ended_ms": T0 + 42 * MIN, "input_sha": SHA["base"],
             "output_sha": SHA["candidate"], "outcome": "pass", "edge": "next",
             "seconds": 50.5, "input_tokens": 1200, "output_tokens": 300,
             "cost_usd": 0.25, "detail": {"kind": "first"}},
            {"stage": "trim", "attempt": 1, "started_ms": T0 + 42 * MIN,
             "ended_ms": T0 + 42 * MIN, "input_sha": SHA["candidate"],
             "output_sha": SHA["candidate"], "outcome": "skipped",
             "edge": "next", "seconds": None, "input_tokens": None,
             "output_tokens": None, "cost_usd": None,
             "detail": {"reason": "[trim] enabled = false"}},
            {"stage": "verify", "attempt": 1, "started_ms": T0 + 43 * MIN,
             "ended_ms": T0 + 44 * MIN, "input_sha": SHA["candidate"],
             "output_sha": SHA["candidate"], "outcome": "pass", "edge": "next",
             "seconds": None, "input_tokens": None, "output_tokens": None,
             "cost_usd": None, "detail": {"round": 1}},
            {"stage": "review", "attempt": 1, "started_ms": T0 + 45 * MIN,
             "ended_ms": None, "input_sha": SHA["candidate"],
             "output_sha": None, "outcome": None, "edge": None,
             "seconds": None, "input_tokens": None, "output_tokens": None,
             "cost_usd": None, "detail": {"round": 1, "adversary": "off"}}])
        [lane] = body["lanes"]
        self.assertEqual({key: lane[key] for key in lane if key != "attempts"},
                         {"run": failed, "attempt": 2, "outcome": "failed",
                          "pipeline_revision": 1})
        self.assertEqual([(a["stage"], a["outcome"], a["edge"])
                          for a in lane["attempts"]],
                         [("implement", "pass", "next"),
                          ("verify", "fail", "end")])

    def test_the_pinned_fixture_is_the_three_run_answer(self):
        _, _, live = self.seed_three_runs()
        self.start()
        self.assertEqual(normalize_contract(self.stages(live)),
                         json.loads(FIXTURE.read_text()))

    def test_a_run_parked_for_approval_serves_its_open_waiting_attempt(self):
        run = self.claim(self.ticket("KO-41"), T0,
                         standard.resolve(self.project))
        park_run(self.conn, run, "awaiting_merge_approval", "PR open",
                 candidate_sha=SHA["candidate"], park_kind="pull_request",
                 pr_url="https://example.test/pull/41", now=T0 + MIN)
        record.waiting(self.conn, run, SHA["candidate"], lambda: "human",
                       ready=True)
        self.conn.commit()
        self.start()
        [waiting] = self.stages(run)["attempts"]
        self.assertEqual((waiting["stage"], waiting["ended_ms"],
                          waiting["outcome"], waiting["edge"],
                          waiting["detail"]["kind"]),
                         ("waiting", None, None, None, "approval"))

    def test_a_run_claimed_before_pipelines_answers_no_pipeline_and_no_attempts(self):
        run = self.claim(self.ticket("KO-42"), T0)
        self.conn.commit()
        self.start()
        self.assertEqual(self.stages(run), {"run": run, "pipeline": None,
                                            "attempts": [], "lanes": []})

    def test_a_non_integer_run_is_400_and_a_missing_run_is_404_naming_it(self):
        self.claim(self.ticket("KO-43"), T0)
        self.conn.commit()
        self.start()
        self.assertEqual(self.request("GET", "/runs/abc/stages")[0], 400)
        code, _, body = self.request("GET", "/runs/999/stages")
        self.assertEqual((code, body["run"]), (404, 999))

    def test_a_configured_secret_in_a_seat_or_an_attempt_detail_is_redacted(self):
        document = standard.resolve(self.project)
        document["stages"][1]["seat"] = f"claude {SECRET}"
        run = self.claim(self.ticket("KO-44"), T0, document)
        self.attempt(run, "implement", T0 + MIN, ("fail", "end", T0 + 2 * MIN),
                     detail={"error": f"push refused for {SECRET}"})
        self.conn.commit()
        (document, detail), = self.conn.execute(
            "SELECT p.document, s.detail FROM pipelines p, stageAttempts s")
        self.assertEqual((json.loads(document)["stages"][1]["seat"],
                          json.loads(detail)["error"]),
                         (f"claude {SECRET}", f"push refused for {SECRET}"))
        self.start(CONFIG + f"\n[extra]\napi_key = '{SECRET}'\n")
        body = self.stages(run)
        self.assertEqual(body["pipeline"]["stages"][1]["seat"],
                         "claude [redacted]")
        self.assertEqual(body["attempts"][0]["detail"]["error"],
                         "push refused for [redacted]")

    def secret_after_bind(self, key):
        run = self.claim(self.ticket(key), T0)
        self.attempt(run, "implement", T0 + MIN, ("fail", "end", T0 + 2 * MIN),
                     detail={"error": f"push refused for {SECRET}"})
        self.conn.commit()
        self.start(CONFIG)
        self.config = self.db.parent / "config.toml"
        self.config.write_text(CONFIG + f"\n[extra]\napi_key = '{SECRET}'\n")
        self.assertEqual(self.stages(run)["attempts"][0]["detail"]["error"],
                         "push refused for [redacted]")
        return run

    def test_a_secret_configured_after_the_daemon_binds_is_redacted(self):
        self.secret_after_bind("KO-46")

    def test_a_rotated_secret_stays_redacted_once_the_daemon_has_read_it(self):
        run = self.secret_after_bind("KO-47")
        self.config.write_text(CONFIG + "\n[extra]\napi_key = 'rotated-0123'\n")
        self.assertEqual(self.stages(run)["attempts"][0]["detail"]["error"],
                         "push refused for [redacted]")

    def test_an_unreadable_config_withholds_the_answer(self):
        for name, broken in (
                ("malformed", f"{CONFIG}\n[extra]\napi_key = '{SECRET}'\n[["
                 .encode()),
                ("latin-1", f"# caf\xe9\n{CONFIG}".encode("latin-1"))):
            with self.subTest(config=name):
                run = self.secret_after_bind(f"KO-{48 + len(name)}")
                self.config.write_bytes(broken)
                code, _, body = self.request("GET", f"/runs/{run}/stages")
                self.assertEqual(code, 503)
                self.assertNotIn("push refused", self.raw_body)
                self.assertIn("error", body)

    def test_a_store_from_before_stage_recording_answers_no_pipeline(self):
        run = self.claim(self.ticket("KO-45"), T0)
        self.conn.commit()
        legacy = sqlite3.connect(str(self.db))
        legacy.executescript("DROP TABLE stageAttempts;\n"
                             "ALTER TABLE runs DROP COLUMN pipelineId;\n"
                             "DROP TABLE pipelines;\n"
                             "PRAGMA user_version = 71;\n")
        legacy.close()
        self.start()
        self.assertEqual(self.stages(run), {"run": run, "pipeline": None,
                                            "attempts": [], "lanes": []})
        (version,) = self.conn.execute("PRAGMA user_version").fetchone()
        self.assertEqual(version, 71)
