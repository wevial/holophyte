"""The "Holophyte standard" pipeline each run is pinned to: the document
resolved from the project's config, the revision a run records against the
real loop and store, what a continuation inherits, a pin that fails without
stopping the run, and the additive migration that adds the record.

Run: python3 -m unittest discover -s tests -p 'test_pipeline_pin.py' -v
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    Commit,
    FakeAgent,
    no_agent_processes,
)
from loop_fixture import Boom, LoopFixture, StubProvider, a_task  # noqa: E402

import holophyte.host.startup  # noqa: E402 - after the sys.path insert above
import holophyte.loop.adjudicate  # noqa: E402 - after the sys.path insert above
import holophyte.loop.gate_build  # noqa: E402 - after the sys.path insert above
import holophyte.loop.implement  # noqa: E402 - after the sys.path insert above
import holophyte.loop.pool  # noqa: E402 - after the sys.path insert above
import holophyte.loop.review_round  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.pipelines  # noqa: E402 - after the sys.path insert above
import store.schema  # noqa: E402 - after the sys.path insert above
from holophyte.stages import standard  # noqa: E402 - after the sys.path insert

CONFIG = """\
[agents]
review_model = "gpt-review-under-test"
review_effort = "high"

[agents.implementer]
harness = "claude"
model = "claude-model-under-test"

[review]
adversary = true

[trim]
enabled = false

[verify]
always = ["ruff check ."]

[merge]
mode = "{mode}"
approve = "human"
ui_capture = "npm run capture"
ui_paths = ["console/**"]
"""

TRIMMED = "[trim]\nenabled = true\n"
UNTRIMMED = "[trim]\nenabled = false\n"


class StandardDocumentTests(ConfigTestCase):
    def resolved(self, mode):
        self.locate(CONFIG.format(mode=mode))
        return standard.resolve(self.project)

    def test_a_pull_request_project_resolves_every_stage_in_order(self):
        document = self.resolved("pr")

        self.assertEqual((document["format"], document["name"]),
                         (1, "Holophyte standard"))
        self.assertEqual(
            [(stage["id"], stage["type"]) for stage in document["stages"]],
            [("critic", "check"), ("implement", "build"), ("trim", "build"),
             ("verify", "check"), ("review", "check"),
             ("merge_gate", "check"), ("capture", "check"),
             ("covering_fix", "build"), ("check_fix", "build"),
             ("capture_fix", "build"), ("waiting", "check")])
        stages = {stage["id"]: stage for stage in document["stages"]}
        self.assertIs(stages["trim"]["switch"], False)
        self.assertIs(stages["review"]["adversary"], True)
        self.assertIn("gpt-review-under-test", stages["review"]["seat"])
        self.assertEqual(stages["verify"]["commands"]["always"],
                         ["ruff check ."])
        self.assertEqual(stages["capture"]["paths"], ["console/**"])
        self.assertEqual(stages["waiting"]["approve"], "human")

    def test_a_reviewer_seat_records_the_effort_its_command_runs_at(self):
        efforts = []
        for effort in ("medium", "xhigh"):
            self.locate('[agents.reviewer]\nharness = "codex"\n'
                        f'model = "gpt-review-under-test"\neffort = "{effort}"\n')
            review, = [stage for stage in standard.resolve(self.project)["stages"]
                       if stage["id"] == "review"]
            efforts.append(review["effort"])

        self.assertEqual(efforts, ["medium", "xhigh"])

    def test_a_reviewer_command_records_the_effort_each_config_spelling_sets(self):
        commands = {
            "--config": "codex --config model_reasoning_effort={} exec -m m",
            "--config=": "codex --config=model_reasoning_effort={} exec -m m",
            "-c": "codex exec -c model_reasoning_effort={} -m m",
            "spaced": "codex exec -c 'model_reasoning_effort = {}' -m m",
            "overridden": "codex exec -c model_reasoning_effort=low"
                          " -c model_reasoning_effort={} -m m",
        }
        for spelling, command in commands.items():
            efforts = []
            for effort in ("medium", "xhigh"):
                self.locate(f'[agents]\nreviewer = "{command.format(effort)}"\n')
                review, = [stage for stage in
                           standard.resolve(self.project)["stages"]
                           if stage["id"] == "review"]
                efforts.append(review["effort"])
            with self.subTest(spelling=spelling):
                self.assertEqual(efforts, ["medium", "xhigh"])

    def test_a_credential_in_a_verify_command_is_not_in_the_document(self):
        with patch.dict(os.environ, {"GH_TOKEN": "ghp-synthetic-credential"}):
            self.locate('[verify]\nbefore_merge = '
                        '["curl -H ghp-synthetic-credential example.invalid"]\n')
            document = standard.resolve(self.project)

        self.assertNotIn("ghp-synthetic-credential", json.dumps(document))
        verify, = [stage for stage in document["stages"]
                   if stage["id"] == "verify"]
        self.assertEqual(len(verify["commands"]["before_merge"]), 1)
        self.assertIn("example.invalid", verify["commands"]["before_merge"][0])

    def test_a_local_project_holds_no_pull_request_stage(self):
        document = self.resolved("local")

        self.assertEqual(
            [stage["id"] for stage in document["stages"]],
            ["critic", "implement", "trim", "verify", "review", "merge_gate",
             "waiting"])


class PinnedPipelineTests(LoopFixture):
    def pins(self):
        return self.read("SELECT r.id, p.revision FROM runs r"
                         " LEFT JOIN pipelines p ON p.id = r.pipelineId"
                         " ORDER BY r.id")

    def test_runs_under_one_config_share_a_revision_until_it_changes(self):
        self.configure(UNTRIMMED)
        self.loop(Commit("first"), APPROVE, Commit("second"), APPROVE,
                  provider=StubProvider(a_task(1), a_task(2)))
        self.configure(TRIMMED)
        self.loop(Commit("third"), APPROVE, provider=StubProvider(a_task(3)))

        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("merged",)] * 3)
        (first,), (second,), (third,) = self.read(
            "SELECT pipelineId FROM runs ORDER BY id")
        self.assertEqual(first, second)
        self.assertEqual(self.pins(), [(1, 1), (2, 1), (3, 2)])

    def test_a_requeued_run_pins_the_changed_config(self):
        self.configure(UNTRIMMED)
        self.loop(Boom())
        self.configure(TRIMMED)
        with sqlite3.connect(self.db) as conn:
            store.requeue(conn, 1, "try again")

        self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("failed",), ("merged",)])
        self.assertEqual(self.pins(), [(1, 1), (2, 2)])

    def test_a_resumed_approved_candidate_keeps_the_parked_runs_pipeline(self):
        self.configure('[merge]\napprove = "human"\n' + UNTRIMMED)
        self.loop(Commit("the scripted work"), APPROVE)
        self.configure('[merge]\napprove = "human"\n' + TRIMMED)
        with sqlite3.connect(self.db) as conn:
            store.approve(conn, 1, "merge it")

        self.loop()

        self.assertEqual(self.read("SELECT id, outcome FROM runs ORDER BY id"),
                         [(1, "abandoned"), (2, "merged")])
        self.assertEqual(self.pins(), [(1, 1), (2, 1)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM pipelines"), [(1,)])

    def test_a_refused_inheritance_names_the_inherit_and_pins_nothing(self):
        self.configure('[merge]\napprove = "human"\n' + UNTRIMMED)
        self.loop(Commit("the scripted work"), APPROVE)
        with sqlite3.connect(self.db) as conn:
            store.approve(conn, 1, "merge it")
        refused = sqlite3.OperationalError("database is locked")

        with patch.object(store.pipelines, "inherit_pipeline",
                          side_effect=refused):
            self.loop()

        self.assertEqual(self.read("SELECT id, outcome FROM runs ORDER BY id"),
                         [(1, "abandoned"), (2, "merged")])
        self.assertEqual(self.pins(), [(1, 1), (2, None)])
        (summary, payload), = self.read(
            "SELECT summary, payload FROM runEvents"
            " WHERE kind = 'stage_record_failed'")
        self.assertIn("inherit_pipeline", summary)
        self.assertEqual(json.loads(payload)["write"], "inherit_pipeline")

    def worker(self, *script, build, env=()):
        fake = FakeAgent(*script)
        with no_agent_processes(), \
                patch.dict(sys.modules,
                           {"holophyte.board.linear_provider": self.provider}), \
                patch.object(holophyte.loop.implement, "agent", fake), \
                patch.object(holophyte.loop.review_round, "agent", fake), \
                patch.object(holophyte.loop.adjudicate, "agent", fake), \
                patch.object(holophyte.host.startup, "BUILD", build), \
                patch.dict(os.environ, {holophyte.loop.pool.WORKER_SLOT_ENV: "2",
                                        **dict(env)}), \
                patch.object(sys, "stdout", io.StringIO()):
            return holophyte.loop.pool.worker(self.project, self.provider)

    def reexecuted(self):
        self.configure(UNTRIMMED)
        self.provider = StubProvider(a_task(1))
        (self.target / "store").mkdir()
        (self.target / "store" / "schema.py").write_text(
            f"SCHEMA_VERSION = {store.schema.SCHEMA_VERSION}\n")
        self.git("add", "store/schema.py")
        self.git("commit", "-q", "-m", "the schema at the checkout's HEAD")
        head = self.git("rev-parse", "HEAD").strip()
        with patch.object(holophyte.loop.gate_build, "EXEC", lambda *a: None):
            self.worker(Commit("the scripted work"), APPROVE, build=self.base)
        return head

    def test_a_gate_reexec_keeps_the_pipeline_its_run_already_had(self):
        head = self.reexecuted()
        (before,), = self.read("SELECT pipelineId FROM runs")
        self.configure(TRIMMED)

        rc = self.worker(build=head, env={"HOLOPHYTE_GATE_RUN": "1"})

        self.assertEqual(rc, holophyte.loop.pool.WORKER_MERGED)
        self.assertEqual(self.read("SELECT pipelineId, outcome FROM runs"),
                         [(before, "merged")])
        self.assertEqual(self.read("SELECT COUNT(*) FROM pipelines"), [(1,)])

    def test_a_gate_reexec_after_a_refused_pin_pins_nothing(self):
        refused = sqlite3.OperationalError("database is locked")
        with patch.object(store.pipelines, "pin_pipeline", side_effect=refused):
            head = self.reexecuted()
        self.configure(TRIMMED)

        rc = self.worker(build=head, env={"HOLOPHYTE_GATE_RUN": "1"})

        self.assertEqual(rc, holophyte.loop.pool.WORKER_MERGED)
        self.assertEqual(self.read("SELECT pipelineId, outcome FROM runs"),
                         [(None, "merged")])
        self.assertEqual(self.read("SELECT COUNT(*) FROM pipelines"), [(0,)])


class FailedPinTests(LoopFixture):
    def walked(self):
        self.configure(UNTRIMMED)
        self.loop(Commit("the scripted work"), APPROVE)
        phases = self.read("SELECT summary FROM runEvents"
                           " WHERE kind = 'phase_change' ORDER BY seq")
        ledger = [(kind, re.sub(r"actual: \S+", "actual: N", text)
                   if kind == "merge" else text)
                  for kind, text in self.read(
                      "SELECT kind, text FROM ledger ORDER BY id")]
        return (phases, ledger,
                self.read("SELECT outcome, pipelineId IS NOT NULL FROM runs"),
                self.read("SELECT summary, payload FROM runEvents"
                          " WHERE kind = 'stage_record_failed'"))

    def test_a_pin_the_store_refuses_leaves_the_run_unchanged(self):
        phases, ledger, runs, failures = self.walked()
        self.doCleanups()
        self.setUp()
        refused = sqlite3.OperationalError("database is locked")
        with patch.object(store.pipelines, "pin_pipeline",
                          side_effect=refused):
            broken = self.walked()

        self.assertEqual((runs, failures), ([("merged", 1)], []))
        self.assertTrue(any(kind == "merge" for kind, _ in ledger), ledger)
        self.assertEqual(broken[:2], (phases, ledger))
        self.assertEqual(broken[2], [("merged", 0)])
        (summary, payload), = broken[3]
        self.assertIn("pin_pipeline", summary)
        self.assertEqual(json.loads(payload)["write"], "pin_pipeline")


class PinPipelineStoreTests(unittest.TestCase):
    def test_a_document_seen_again_keeps_its_first_revision_and_time(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = store.open(Path(directory) / "store.db")
            self.addCleanup(conn.close)
            project = store.ensure_project(conn, "team-1", "/repo-1")
            runs = []
            for number in (1, 2, 3):
                ticket = store.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{number}",
                    linear_identifier=f"KO-{number}", title="candidate")
                runs.append(store.claim(conn, project, ticket))
            first = {"format": 1, "stages": [{"id": "trim", "switch": False}]}
            second = {"format": 1, "stages": [{"id": "trim", "switch": True}]}

            store.pipelines.pin_pipeline(conn, runs[0], "n", first, now=123)
            store.pipelines.pin_pipeline(conn, runs[1], "n", second, now=456)
            store.pipelines.pin_pipeline(conn, runs[2], "n", first, now=789)

            self.assertEqual(conn.execute(
                "SELECT r.id, p.revision, p.createdAt FROM runs r"
                " JOIN pipelines p ON p.id = r.pipelineId ORDER BY r.id"
            ).fetchall(), [(runs[0], 1, 123), (runs[1], 2, 456),
                           (runs[2], 1, 123)])


class PipelineMigrationTests(unittest.TestCase):
    def test_a_previous_version_store_gains_the_table_and_a_null_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = store.open(path)
            project = store.ensure_project(conn, "team-1", "/repo-1")
            ticket = store.mirror_ticket(
                conn, project, linear_issue_id="issue-1",
                linear_identifier="KO-1", title="candidate")
            run = store.claim(conn, project, ticket)
            store.release(conn, run, "failed", "scripted")
            conn.close()
            raw = sqlite3.connect(path)
            raw.executescript("ALTER TABLE runs DROP COLUMN pipelineId;\n"
                              "DROP TABLE pipelines;\n"
                              "PRAGMA user_version = 71;\n")
            raw.close()

            conn = store.open(path)
            self.addCleanup(conn.close)

            self.assertEqual(conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'pipelines'"
            ).fetchall(), [("pipelines",)])
            self.assertEqual(conn.execute(
                "SELECT outcome, pipelineId FROM runs").fetchall(),
                [("failed", None)])
            self.assertEqual(store.schema.SCHEMA_VERSION, 73)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                             (73,))
            self.assertEqual(store.schema.READABLE_FROM, 50)


if __name__ == "__main__":
    unittest.main()
