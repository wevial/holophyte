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
import store.pipelines  # noqa: E402 - after the sys.path insert above
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
        """Judged reviews of 5 to 40 s, a metered 60 s error, two skips, an
        open review and one older than the window. Fails of 10, 15 and 35 s
        are each next followed in their own run by a build that commits: the
        15 s one shares its start with that build, and the 35 s one was
        recorded after it though it started first. The 20 s fail's fix makes
        no commit, the 5 s fail leaves by `end`, the 30 s fail is next
        followed by a check that moves the commit, and the 25 s fail ends its
        run just before another run's committing build."""
        run = self.run_of("KO-1")
        start = self.now - 10 * 60 * MIN
        moved = (SHA["candidate"], SHA["fixed"])
        steps = [
            ("review", 10, ("fail", "on_fail"), None, {"findings": 3},
             {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.5}),
            ("implement", 30, ("pass", "next"), moved, None, None),
            ("review", 20, ("fail", "on_fail"), None, {"findings": 2},
             {"input_tokens": 200, "output_tokens": 400}),
            ("implement", 30, ("pass", "next"), None, None, None),
            ("review", 5, ("fail", "end"), None, {"findings": 7},
             {"input_tokens": 300, "output_tokens": 10, "cost_usd": 1.0}),
            ("implement", 30, ("pass", "next"), moved, None, None),
            ("review", 30, ("fail", "on_fail"), None, {"findings": 4}, None),
            ("verify", 30, ("pass", "next"), moved, None, None),
            ("implement", 30, ("pass", "next"), moved, None, None),
            ("review", 40, ("pass", "next"), None, None, {"cost_usd": 4.0}),
            ("review", 15, ("fail", "on_fail"), None, {"findings": 5}, None),
        ]
        for minute, (stage, took, closed, sha, detail, usage) in enumerate(
                steps):
            self.attempt(run, stage, start + minute * MIN, took * SEC, closed,
                         sha or (SHA["candidate"], SHA["candidate"]), detail,
                         usage)
        self.attempt(run, "implement", start + 10 * MIN, 30 * SEC,
                     ("pass", "next"), moved)
        cut = self.run_of("KO-8")
        self.attempt(cut, "review", start + 12 * MIN, 25 * SEC,
                     ("fail", "on_fail"), detail={"findings": 11})
        after = self.run_of("KO-9")
        self.attempt(after, "implement", start + 13 * MIN, 30 * SEC,
                     ("pass", "next"), moved)
        late = self.run_of("KO-10")
        self.attempt(late, "implement", start + 15 * MIN, 30 * SEC,
                     ("pass", "next"), moved)
        self.attempt(late, "review", start + 14 * MIN, 35 * SEC,
                     ("fail", "on_fail"), detail={"findings": 6})
        other = self.run_of("KO-2")
        self.attempt(other, "review", start + 20 * MIN, 60 * SEC,
                     ("error", "end"),
                     usage={"input_tokens": 900, "output_tokens": 100,
                            "cost_usd": 2.0})
        for minute in (21, 22):
            store.stages.skip_stage(self.conn, other, "review", "not needed",
                                    now=start + minute * MIN)
        self.attempt(other, "review", start + 23 * MIN)
        stale = self.run_of("KO-3")
        self.attempt(stale, "review", self.now - 40 * DAY, 90 * SEC,
                     ("fail", "on_fail"), detail={"findings": 9},
                     usage={"input_tokens": 9000, "output_tokens": 9000,
                            "cost_usd": 9.0})
        self.attempt(stale, "implement", self.now - 40 * DAY + MIN, SEC,
                     ("pass", "next"), sha=moved)
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
            "id": "review", "in_pipeline": True, "attempts": 11,
            "median_seconds": 25, "median_tokens": 455,
            "median_cost_usd": 1.5, "fail_rate": 7 / 9, "findings_fixed": 14,
            "outcomes": {"pass": 1, "fail": 7, "skipped": 2, "error": 1}})

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
        for age in (3 * DAY, 20 * DAY):
            self.attempt(run, "verify", self.now - age, SEC, ("pass", "next"))
        self.attempt(run, "verify", self.now - 8 * DAY, 2 * DAY,
                     ("pass", "next"))
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
                         [("verify", 3)])

    def test_a_sibling_projects_attempts_and_revision_are_never_served(self):
        other = store.tickets.ensure_project(self.conn, "team-2",
                                             self.root / "other")
        ticket = store.tickets.mirror_ticket(
            self.conn, other, linear_issue_id="issue-KO-6",
            linear_identifier="KO-6", title="ticket KO-6",
            acceptance_criteria=["Given KO-6, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=60 * MIN)
        store.tickets.transition(self.conn, ticket, "in_flight")
        run = store.claim(self.conn, other, ticket)
        current = holophyte.config.project.Project.locate(self.target)
        store.pipelines.pin_pipeline(self.conn, run, standard.NAME,
                                     standard.resolve(current))
        self.attempt(run, "review", self.now - MIN, SEC, ("fail", "end"))
        self.conn.commit()
        for name, board in (
                ("no board", ""),
                ("the sibling's team",
                 '[board]\nteam = "team-2"\nproject_id = "board-2"\n')):
            with self.subTest(config=name):
                (self.db.parent / "config.toml").write_text(CONFIG + board)
                body = self.answer()
                self.assertEqual((body["pipeline"]["revision"],
                                  body["stages"]), (None, []))

    def test_the_revision_is_the_configured_boards_project_row(self):
        board = '[board]\nteam = "team-2"\nproject_id = "board-2"\n'
        (self.db.parent / "config.toml").write_text(CONFIG + board)
        self.project = holophyte.config.project.Project.locate(self.target)
        self.project_id = store.tickets.ensure_project(self.conn, "team-2",
                                                       self.target)
        store.pipelines.pin_pipeline(self.conn, self.run_of("KO-7"),
                                     standard.NAME,
                                     standard.resolve(self.project))
        self.conn.commit()
        self.assertEqual(self.answer()["pipeline"]["revision"], 1)

    def test_a_setting_the_pipeline_cannot_resolve_is_503_naming_only_it(self):
        credential = "ghp-synthetic-credential-0123456789"
        (self.db.parent / "config.toml").write_text(
            CONFIG + f'[trim]\nenabled = "{credential}"\n')
        self.start()
        code, _, body = self.request("GET", "/pipeline")
        self.assertEqual(code, 503)
        self.assertIn("[trim] enabled", body["error"])
        self.assertNotIn(credential, self.raw_body)

    def test_days_outside_1_to_365_is_400_naming_the_range(self):
        self.start()
        for days in ("0", "366", "x"):
            with self.subTest(days=days):
                code, _, body = self.request("GET", f"/pipeline?days={days}")
                self.assertEqual(code, 400)
                self.assertIn("from 1 to 365", body["error"])


class ConfigErrorTests(ServeTestCase):
    def test_the_error_names_only_a_setting_the_config_holds(self):
        state = self.root / "state[workspace]"
        state.mkdir()
        project = holophyte.config.project.Project(
            path=self.target, holo_dir=state, store_path=state / "store.db",
            config_path=state / "config.toml",
            worktrees=self.root / "repo.worktrees")
        isolation = CONFIG.replace(
            "[agents]\n", '[agents]\nimplementer_isolation = '
            '{ backend = "container", writable = "yes" }\n')
        for config, named in ((CONFIG + '[trim]\nenabled = "sk-0123"\n',
                               ": check [trim] enabled"),
                              ("trim = false\n" + CONFIG, ": check [trim]"),
                              (isolation,
                               ": check [agents] implementer_isolation")):
            with self.subTest(config=named):
                project.config_path.write_text(config)
                code, body = pipeline(project, "")
                self.assertEqual(code, 503)
                self.assertTrue(body["error"].endswith(named), body["error"])


    def test_a_private_pattern_in_the_named_setting_is_redacted(self):
        (self.db.parent / "config.toml").write_text(
            CONFIG + '[merge]\nprivate_patterns = ["acme-internal"]\n'
            '[agents.implementer_profiles.acme-internal]\n'
            'harness = "no-such-harness"\nmodel = "m"\n')
        project = holophyte.config.project.Project.locate(self.target)
        code, body = pipeline(project, "")
        self.assertEqual(code, 503)
        self.assertIn("[agents.implementer_profiles.[redacted]]",
                      body["error"])
        self.assertNotIn("acme-internal", json.dumps(body))


class SharedProcessTests(ServeTestCase):
    def target_with(self, name, config):
        target = self.root / name
        target.mkdir()
        project = holophyte.config.project.Project.locate(target)
        project.config_path.parent.mkdir(parents=True)
        project.config_path.write_text(CONFIG + config)
        return project

    def test_another_projects_private_patterns_never_redact_this_pipeline(self):
        command = "curl https://beta-host.example/ping"
        verify = f'[verify]\nalways = ["{command}"]\n'
        beta = self.target_with("beta", verify)
        alpha = self.target_with(
            "alpha", verify + '[merge]\nprivate_patterns = ["beta-host"]\n')
        conn = store.open(str(beta.store_path))
        self.addCleanup(conn.close)
        store.init(conn)
        project_id = store.tickets.ensure_project(conn, "team-b", beta.path)
        ticket = store.tickets.mirror_ticket(
            conn, project_id, linear_issue_id="issue-KO-11",
            linear_identifier="KO-11", title="ticket KO-11",
            acceptance_criteria=["Given KO-11, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=60 * MIN)
        store.tickets.transition(conn, ticket, "in_flight")
        run = store.claim(conn, project_id, ticket)
        store.pipelines.pin_pipeline(conn, run, standard.NAME,
                                     standard.resolve(beta))
        conn.commit()

        def verify_of(project):
            code, body = pipeline(project, "")
            self.assertEqual(code, 200)
            stage, = [stage for stage in body["pipeline"]["stages"]
                      if stage["id"] == "verify"]
            return body["pipeline"]["revision"], stage["commands"]["always"]

        self.assertEqual(verify_of(beta), (1, [command]))
        self.assertNotIn("beta-host", json.dumps(verify_of(alpha)))
        self.assertEqual(verify_of(beta), (1, [command]))


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
