"""A project's `[[pipeline.check]]` stages: the entries the config accepts,
the check stages a run pins in its pipeline, the advisory and off checks a
run records as stage attempts, and a checks module that fails without
changing the run.

Run: python3 -m unittest discover -s tests -p 'test_pipeline_checks.py' -v
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    REQUEST_CHANGES,
    Commit,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    UNTRIMMED,
    LoopFixture,
    StubProvider,
    a_task,
)

from holophyte.config.checks import check_config  # noqa: E402
from holophyte.config.pipeline_settings import pipeline_checks  # noqa: E402
from holophyte.stages import checks  # noqa: E402 - after the sys.path insert

# Fixed commit times, so two fixtures' candidates have the same shas.
GIT_DATES = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
             "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
FAILING_LINT = ["echo lint says no", "exit 1"]


def check_table(stage_id, after, commands, mode, paths=None):
    return ("[[pipeline.check]]\n"
            f"id = {json.dumps(stage_id)}\nafter = {json.dumps(after)}\n"
            f"commands = {json.dumps(commands)}\nmode = {json.dumps(mode)}\n"
            + (f"paths = {json.dumps(paths)}\n" if paths is not None else ""))


class ChecksConfigTests(LoopFixture):
    def test_entries_with_every_key_load(self):
        self.configure(check_table("lint", "implement", ["ruff check ."],
                                   "advisory")
                       + check_table("docs", "trim", ["make docs", "true"],
                                     "off", ["docs/**"]))

        check_config(self.project)

        self.assertEqual(pipeline_checks(self.project), [
            {"id": "lint", "after": "implement", "mode": "advisory",
             "commands": ["ruff check ."], "paths": []},
            {"id": "docs", "after": "trim", "mode": "off",
             "commands": ["make docs", "true"], "paths": ["docs/**"]}])

    def test_a_bad_entry_stops_the_load_naming_the_entry(self):
        good = check_table("first", "implement", ["true"], "advisory")
        for case, config, problem in (
                ("repeated id", good + check_table(
                    "first", "trim", ["true"], "off"), "repeats"),
                ("standard id", check_table(
                    "verify", "trim", ["true"], "off"), "standard stage"),
                ("after", check_table(
                    "first", "review", ["true"], "off"), "after must be"),
                ("commands", check_table(
                    "first", "trim", [], "off"), "commands must be"),
                ("mode", check_table(
                    "first", "trim", ["true"], "blocking"), "mode must be"),
                ("absolute path", check_table(
                    "first", "trim", ["true"], "off", ["/etc/*"]), "paths"),
                ("climbing path", check_table(
                    "first", "trim", ["true"], "off", ["docs/../../x"]),
                 "paths")):
            with self.subTest(case=case):
                self.configure(config)
                with self.assertRaises(SystemExit) as stopped:
                    check_config(self.project)
                message = str(stopped.exception)
                self.assertIn("[[pipeline.check]] "
                              + ("verify" if case == "standard id" else "first"),
                              message)
                self.assertIn(problem, message)

    def test_the_required_mode_is_refused_as_not_available_yet(self):
        self.configure(check_table("types", "implement", ["mypy ."],
                                   "required"))

        with self.assertRaises(SystemExit) as stopped:
            check_config(self.project)

        message = str(stopped.exception)
        self.assertIn("[[pipeline.check]] types", message)
        self.assertIn("mode required is not available yet", message)


class CheckRuns(LoopFixture):
    def attempts(self, run_id=1):
        return [(stage, sha, outcome, edge, json.loads(detail))
                for stage, sha, outcome, edge, detail in self.read(
                    "SELECT stageId, inputSha, outcome, edge, detail"
                    f" FROM stageAttempts WHERE runId = {run_id} ORDER BY id")]

    def walked(self, config, *script):
        self.configure(UNTRIMMED + config)
        with patch.dict(os.environ, GIT_DATES):
            fake, _ = self.loop(*script)
        phases = self.read("SELECT summary FROM runEvents"
                           " WHERE kind = 'phase_change' ORDER BY seq")
        ledger = [(kind, re.sub(r"\d+(\.\d+)? ?min", "N min", text))
                  for kind, text in self.read(
                      "SELECT kind, text FROM ledger ORDER BY id")]
        return ([turn.goal for turn in fake.turns], phases, ledger,
                self.read("SELECT outcome FROM runs"))

    def fresh(self):
        self.doCleanups()
        self.setUp()


class PinnedChecksTests(CheckRuns):
    def pins(self):
        return [(revision, json.loads(document)) for revision, document in
                self.read("SELECT p.revision, p.document FROM runs r"
                          " JOIN pipelines p ON p.id = r.pipelineId"
                          " ORDER BY r.id")]

    def test_checks_pin_after_their_build_stage_and_none_pins_the_same_revision(self):
        config = self.db.parent / "config.toml"
        checked = (UNTRIMMED
                   + check_table("lint", "implement", ["true"], "advisory")
                   + check_table("docs", "trim", ["make docs"], "off",
                                 ["docs/**"]))
        self.configure(UNTRIMMED)
        self.loop(Commit("first"), APPROVE, provider=StubProvider(a_task(1)))
        config.write_text(checked)
        self.loop(Commit("second"), APPROVE, provider=StubProvider(a_task(2)))
        config.write_text(UNTRIMMED)
        self.loop(Commit("third"), APPROVE, provider=StubProvider(a_task(3)))

        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("merged",)] * 3)
        (first, plain), (second, pinned), (third, again) = self.pins()
        self.assertEqual((first, second, third), (1, 2, 1))
        self.assertEqual(again, plain)
        ids = [stage["id"] for stage in pinned["stages"]]
        self.assertEqual(ids[ids.index("implement"):ids.index("verify")],
                         ["implement", "lint", "trim", "docs"])
        by_id = {stage["id"]: stage for stage in pinned["stages"]}
        self.assertEqual(
            {key: by_id["lint"][key] for key in
             ("type", "after", "mode", "commands", "paths")},
            {"type": "check", "after": "implement", "mode": "advisory",
             "commands": ["true"], "paths": []})
        self.assertEqual(
            {key: by_id["docs"][key] for key in
             ("type", "after", "mode", "commands", "paths")},
            {"type": "check", "after": "trim", "mode": "off",
             "commands": ["make docs"], "paths": ["docs/**"]})
        self.assertEqual(
            {**pinned, "stages": [stage for stage in pinned["stages"]
                                  if stage["id"] not in ("lint", "docs")]},
            plain)


class AdvisoryCheckTests(CheckRuns):
    SCRIPT = (Commit("the scripted work"), REQUEST_CHANGES,
              Commit("the scripted fix"), APPROVE)

    def test_a_failing_advisory_check_is_recorded_each_round_and_changes_nothing(self):
        plain = self.walked("", *self.SCRIPT)
        self.fresh()
        linted = self.walked(check_table("lint", "implement", FAILING_LINT,
                                         "advisory"), *self.SCRIPT)

        self.assertEqual(linted[3], [("merged",)])
        self.assertEqual(linted, plain)
        attempts = self.attempts()
        lints = [at for at, row in enumerate(attempts) if row[0] == "lint"]
        self.assertEqual(len(lints), 2)
        for rnd, at in enumerate(lints, 1):
            _, sha, outcome, edge, detail = attempts[at]
            following = attempts[at + 1:]
            verify = next(row for row in following if row[0] == "verify")
            review = next(row for row in following if row[0] == "review")
            self.assertEqual((outcome, edge), ("fail", "next"))
            self.assertEqual(detail["exit_status"], 1)
            self.assertEqual(detail["round"], rnd)
            self.assertIn("lint says no", detail["output"])
            self.assertEqual(attempts[at + 1][0], "verify")
            self.assertEqual(sha, verify[1])
            self.assertEqual(sha, review[1])
            self.assertEqual(review[4]["round"], rnd)

    def test_a_passing_advisory_check_records_pass(self):
        self.walked(check_table("lint", "implement", ["echo clean"],
                                "advisory"), Commit("the scripted work"),
                    APPROVE)

        (lint,) = [row for row in self.attempts() if row[0] == "lint"]
        self.assertEqual(lint[2:4], ("pass", "next"))
        self.assertEqual(lint[4]["exit_status"], 0)
        self.assertIn("clean", lint[4]["output"])


class SkippedCheckTests(CheckRuns):
    def test_an_unmatched_path_condition_and_mode_off_skip_without_running(self):
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        markers = Path(held.name)

        def touch(name):
            return [f"touch {markers / name}"]

        self.walked(check_table("docs", "implement", touch("docs"), "advisory",
                                ["docs/**"])
                    + check_table("scripted", "implement", touch("scripted"),
                                  "advisory", ["scripted-*.txt"])
                    + check_table("dormant", "trim", touch("dormant"), "off"),
                    Commit("the scripted work"), APPROVE)

        found = {row[0]: row for row in self.attempts()}
        self.assertEqual(found["docs"][2], "skipped")
        self.assertIn("docs/**", found["docs"][4]["reason"])
        self.assertEqual(found["dormant"][2], "skipped")
        self.assertEqual(found["dormant"][4], {"reason": "mode off"})
        self.assertEqual(found["scripted"][2:4], ("pass", "next"))
        self.assertEqual(sorted(path.name for path in markers.iterdir()),
                         ["scripted"])


class FailedChecksTests(CheckRuns):
    SCRIPT = AdvisoryCheckTests.SCRIPT

    def test_a_raising_checks_module_sends_the_prompts_of_a_run_without_checks(self):
        plain = self.walked("", *self.SCRIPT)
        self.fresh()
        refused = RuntimeError("checks unavailable")
        raising = [patch.object(checks, name, side_effect=refused)
                   for name, value in vars(checks).items()
                   if callable(value)
                   and getattr(value, "__module__", None) == checks.__name__]
        for patched in raising:
            patched.start()
            self.addCleanup(patched.stop)
        broken = self.walked(
            check_table("lint", "implement", FAILING_LINT, "advisory")
            + check_table("docs", "trim", FAILING_LINT, "advisory"),
            *self.SCRIPT)

        self.assertEqual(plain[3], [("merged",)])
        self.assertEqual(len(plain[0]), 4)
        self.assertEqual(broken, plain)
        writes = [json.loads(payload)["write"] for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'stage_record_failed'")]
        self.assertEqual(writes, ["pipeline_checks"] * 3)
        self.assertNotIn("lint", [row[0] for row in self.attempts()])


if __name__ == "__main__":
    unittest.main()
