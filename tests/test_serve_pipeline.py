"""`GET /pipeline`: the project's pipeline as its config resolves now, its
pinned revision when a run holds that document, and each stage's numbers
over a window of days, read from a real store through the daemon.

Run: python3 -m unittest discover -s tests -p 'test_serve_pipeline.py' -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from time import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import test_serve  # noqa: E402 - after the sys.path insert above
from fake_agent import APPROVE, Commit  # noqa: E402 - after the sys.path insert
from loop_fixture import LoopFixture  # noqa: E402 - after the sys.path insert
from serve_fixture import MIN, SEC, ServeTestCase  # noqa: E402

import holophyte.config.project  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.stages  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from holophyte.serve.serve_pipeline import pipeline  # noqa: E402
from holophyte.stages import standard  # noqa: E402 - after the sys.path insert
from tests.serve_contract_fixture import normalize_contract  # noqa: E402

DAY = 24 * 60 * MIN
SHA = {name: name[0] * 40 for name in ("base", "candidate", "fixed")}
CONFIG = """\
[agents]
review_model = "gpt-review-under-test"
review_effort = "high"

[agents.implementer]
harness = "claude"
model = "claude-model-under-test"
"""
UNTRIMMED = "[trim]\nenabled = false\n"
TRIMMED = "[trim]\nenabled = true\n"
FIXTURE = HERE / "fixtures" / "serve" / "pipeline.json"


class CurrentDocumentTests(LoopFixture):
    def test_the_pinned_document_carries_its_revision_until_the_config_moves(self):
        self.configure(UNTRIMMED)
        self.loop(Commit("the scripted work"), APPROVE)
        (document, revision), = self.read(
            "SELECT p.document, p.revision FROM runs r"
            " JOIN pipelines p ON p.id = r.pipelineId")

        code, body = pipeline(self.project, "")
        self.assertEqual(code, 200)
        self.assertEqual(body["pipeline"],
                         {**json.loads(document), "revision": revision})

        (self.db.parent / "config.toml").write_text(TRIMMED)
        code, body = pipeline(self.project, "")
        self.assertEqual(code, 200)
        moved = holophyte.config.project.Project.locate(self.target)
        self.assertEqual(body["pipeline"],
                         {**standard.resolve(moved), "revision": None})
        trim, = [stage for stage in body["pipeline"]["stages"]
                 if stage["id"] == "trim"]
        self.assertIs(trim["switch"], True)


class StageNumbersTests(ServeTestCase):
    maxDiff = None

    def setUp(self):
        super().setUp()
        (self.db.parent / "config.toml").write_text(CONFIG)
        self.now = int(time() * 1000)
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(self.conn, "team-1",
                                                       self.target)

    def run_of(self, key):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{key}",
            linear_identifier=key, title=f"ticket {key}",
            acceptance_criteria=[f"Given {key}, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=60 * MIN)
        store.tickets.transition(self.conn, ticket, "in_flight")
        return store.claim(self.conn, self.project_id, ticket)

    def attempt(self, run, stage, started, took=None, closed=None,
                sha=(SHA["candidate"], SHA["candidate"]), detail=None,
                usage=None):
        attempt = store.stages.open_stage(self.conn, run, stage, sha[0],
                                          detail, now=started)
        if usage is not None:
            store.record_event(
                self.conn, run, "agent_turn", f"{stage} turn ended",
                level="detail", payload=json.dumps(
                    dict(role=stage, seconds=1, usage=usage)))
        if closed is not None:
            store.stages.close_stage(self.conn, attempt, *closed, sha[1],
                                     [stage], now=started + took)

    def seed_reviews(self):
        """Three judged reviews of 10, 20 and 40 s, a 20 s error, a skip, an
        open review and one older than the window; the first fail's fix
        commits, the second's makes no commit."""
        run = self.run_of("KO-1")
        start = self.now - 10 * 60 * MIN
        self.attempt(run, "review", start, 10 * SEC, ("fail", "on_fail"),
                     detail={"findings": 3},
                     usage={"input_tokens": 100, "output_tokens": 50,
                            "cost_usd": 0.5})
        self.attempt(run, "implement", start + MIN, 30 * SEC, ("pass", "next"),
                     sha=(SHA["candidate"], SHA["fixed"]))
        self.attempt(run, "review", start + 2 * MIN, 20 * SEC,
                     ("fail", "on_fail"), detail={"findings": 2},
                     usage={"input_tokens": 200, "output_tokens": 100})
        self.attempt(run, "implement", start + 3 * MIN, 30 * SEC,
                     ("pass", "next"), sha=(SHA["fixed"], SHA["fixed"]))
        self.attempt(run, "review", start + 4 * MIN, 40 * SEC,
                     ("pass", "next"),
                     usage={"input_tokens": 400, "output_tokens": 200,
                            "cost_usd": 1.5})
        other = self.run_of("KO-2")
        self.attempt(other, "review", start + 5 * MIN, 20 * SEC,
                     ("error", "end"))
        store.stages.skip_stage(self.conn, other, "review", "not needed",
                                now=start + 6 * MIN)
        self.attempt(other, "review", start + 7 * MIN)
        stale = self.run_of("KO-3")
        self.attempt(stale, "review", self.now - 40 * DAY, 90 * SEC,
                     ("fail", "on_fail"), detail={"findings": 9},
                     usage={"input_tokens": 9000, "output_tokens": 9000,
                            "cost_usd": 9.0})
        self.attempt(stale, "implement", self.now - 40 * DAY + MIN, SEC,
                     ("pass", "next"), sha=(SHA["candidate"], SHA["fixed"]))
        self.conn.commit()

    def answer(self, query=""):
        self.start()
        code, _, body = self.request("GET", f"/pipeline{query}")
        self.assertEqual(code, 200, body)
        return body

    def test_a_reviews_numbers_count_closed_attempts_inside_the_window(self):
        self.seed_reviews()
        review, = [stage for stage in self.answer()["stages"]
                   if stage["id"] == "review"]
        self.assertEqual(review, {
            "id": "review", "in_pipeline": True, "attempts": 5,
            "median_seconds": 20, "median_tokens": 300,
            "median_cost_usd": 1.0, "fail_rate": 0.5, "findings_fixed": 3,
            "outcomes": {"pass": 1, "fail": 2, "skipped": 1, "error": 1}})

    def test_the_pinned_fixture_is_the_review_answer(self):
        self.seed_reviews()
        body = self.answer()
        body["window"]["since_ms"] = 1
        self.assertEqual(normalize_contract(body),
                         json.loads(FIXTURE.read_text()))

    def test_a_stage_the_document_no_longer_holds_is_listed_after_it(self):
        run = self.run_of("KO-4")
        self.attempt(run, "retired_gate", self.now - MIN, SEC,
                     ("pass", "next"))
        self.attempt(run, "verify", self.now - 2 * MIN, SEC, ("pass", "next"))
        self.conn.commit()
        self.assertEqual([(stage["id"], stage["in_pipeline"])
                          for stage in self.answer()["stages"]],
                         [("verify", True), ("retired_gate", False)])

    def test_days_narrows_the_window_to_attempts_started_inside_it(self):
        run = self.run_of("KO-5")
        for age in (3 * DAY, 10 * DAY):
            self.attempt(run, "verify", self.now - age, SEC, ("pass", "next"))
        self.conn.commit()
        before = int(time() * 1000)
        body = self.answer("?days=7")
        after = int(time() * 1000)
        self.assertEqual([(stage["id"], stage["attempts"])
                          for stage in body["stages"]], [("verify", 1)])
        self.assertEqual(body["window"]["days"], 7)
        self.assertTrue(before - 7 * DAY <= body["window"]["since_ms"]
                        <= after - 7 * DAY)
        self.assertEqual([(stage["id"], stage["attempts"])
                          for stage in self.answer()["stages"]],
                         [("verify", 2)])

    def test_days_outside_1_to_365_is_400_naming_the_range(self):
        self.start()
        for days in ("0", "366", "x"):
            with self.subTest(days=days):
                code, _, body = self.request("GET", f"/pipeline?days={days}")
                self.assertEqual(code, 400)
                self.assertIn("from 1 to 365", body["error"])


class NoStoreTests(ServeTestCase):
    def test_a_project_with_no_store_answers_the_document_and_no_stages(self):
        self.start(CONFIG)
        code, _, body = self.request("GET", "/pipeline")
        self.assertEqual(code, 200)
        self.assertFalse(self.db.exists())
        self.assertEqual(body["pipeline"],
                         {**standard.resolve(self.project), "revision": None})
        self.assertEqual(body["stages"], [])


class ReadTokenTests(ServeTestCase):
    def test_the_read_token_reads_the_pipeline_while_config_stays_hidden(self):
        path = self.root / "serve.token"
        path.write_text(test_serve.TokenTests.TOKEN + "\n")
        path.chmod(0o600)
        self.start(f'[serve]\ntoken_file = "{path}"\nconfig_edit = false\n',
                   host="0.0.0.0")
        bearer = test_serve.TokenTests.BEARER
        self.assertEqual(self.request("GET", "/pipeline")[0], 401)
        self.assertEqual(self.request("GET", "/pipeline", bearer)[0], 200)
        self.assertEqual(self.request("GET", "/config", bearer)[0], 404)
