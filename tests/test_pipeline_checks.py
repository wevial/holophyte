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
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_stale_approval_rereview as stale_fixture  # noqa: E402
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    REQUEST_CHANGES,
    Commit,
    Idle,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    UNTRIMMED,
    LoopFixture,
    StubProvider,
    a_task,
)
from test_pipeline_notes import (  # noqa: E402 - after the sys.path insert
    WITNESS,
    EditsConfig,
    TrimTurn,
    lines,
)

from holophyte.config.checks import check_config  # noqa: E402
from holophyte.config.pipeline_settings import (  # noqa: E402
    STANDARD_STAGES,
    pipeline_checks,
)
from holophyte.stages import checks, standard  # noqa: E402

# Fixed commit times, so two fixtures' candidates have the same shas.
GIT_DATES = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
             "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
# A head over the recorded tail, then a listing only the task worktree has.
# The document and digest the resolver pinned for this fixture before any
# `[[pipeline.check]]` existed.
PRE_CHECKS_DIGEST = (
    "fcbf7a0f0a8573ed04c03c89ea65eba024cfc7339ae9175c7fac7c7054cfe4f9")
PRE_CHECKS_DOCUMENT = {
    "format": 1, "merge": {"approve": "auto", "mode": "local"},
    "name": "Holophyte standard",
    "stages": [
        {"group": "ticket", "id": "critic", "roles": [], "seat": None,
         "switch": False, "type": "check"},
        {"group": "candidate", "id": "implement", "profile": None,
         "roles": ["implement"], "seat": "claude opus", "type": "build"},
        {"group": "candidate", "id": "trim", "roles": ["trim"],
         "seat": "claude opus", "switch": False, "type": "build"},
        {"commands": {"always": [], "before_merge": []},
         "group": "candidate", "id": "verify", "roles": [], "type": "check"},
        {"adversary": False, "effort": "high", "group": "candidate",
         "id": "review",
         "roles": ["review", "adjudicate", "adversary", "consolidate"],
         "seat": "codex gpt-6-astra", "type": "check"},
        {"group": "factory", "id": "merge_gate", "roles": [],
         "type": "check"},
        {"approve": "auto", "group": "factory", "id": "waiting", "roles": [],
         "type": "check"}]}
FAILING_LINT = ["printf 'START%01200d\\n' 0", "ls scripted-*.txt", "exit 1"]


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
        for case, config, name, problem in (
                ("repeated id", good + check_table(
                    "first", "trim", ["true"], "off"), "first", "repeats"),
                ("standard id", check_table(
                    "verify", "trim", ["true"], "off"), "verify",
                 "standard stage"),
                ("after", check_table(
                    "first", "review", ["true"], "off"), "first", "after must be"),
                ("commands", check_table(
                    "first", "trim", [], "off"), "first", "commands must be"),
                ("mode", check_table(
                    "first", "trim", ["true"], "blocking"), "first", "mode must be"),
                ("absolute path", check_table(
                    "first", "trim", ["true"], "off", ["/etc/*"]), "first", "paths"),
                ("climbing path", check_table(
                    "first", "trim", ["true"], "off", ["docs/../../x"]),
                 "first", "paths")):
            with self.subTest(case=case):
                self.configure(config)
                with self.assertRaises(SystemExit) as stopped:
                    check_config(self.project)
                message = str(stopped.exception)
                self.assertIn(f"[[pipeline.check]] {name}", message)
                self.assertIn(problem, message)

    def test_every_stage_the_standard_pipeline_resolves_is_a_reserved_id(self):
        self.configure('[merge]\nmode = "pr"\n')

        ids = [stage["id"] for stage in standard.resolve(self.project)["stages"]]

        self.assertEqual(sorted(ids), sorted(STANDARD_STAGES))

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
        self.assertEqual(plain, PRE_CHECKS_DOCUMENT)
        self.assertEqual(again, plain)
        self.assertEqual(self.read("SELECT digest FROM pipelines"
                                   " WHERE revision = 1"),
                         [(PRE_CHECKS_DIGEST,)])
        ids = [stage["id"] for stage in pinned["stages"]]
        self.assertEqual(ids[ids.index("implement"):ids.index("verify")],
                         ["implement", "lint", "trim", "docs"])
        by_id = {stage["id"]: stage for stage in pinned["stages"]}
        self.assertEqual(
            {stage_id: {key: by_id[stage_id][key] for key in
                        ("type", "after", "mode", "commands", "paths")}
             for stage_id in ("lint", "docs")},
            {"lint": {"type": "check", "after": "implement", "mode": "advisory",
                      "commands": ["true"], "paths": []},
             "docs": {"type": "check", "after": "trim", "mode": "off",
                      "commands": ["make docs"], "paths": ["docs/**"]}})
        self.assertEqual(
            {**pinned, "stages": [stage for stage in pinned["stages"]
                                  if stage["id"] not in ("lint", "docs")]},
            plain)


class AdvisoryCheckTests(CheckRuns):
    SCRIPT = (Commit("the scripted work"), REQUEST_CHANGES,
              Commit("the scripted fix"), APPROVE)

    def test_a_failing_advisory_check_is_recorded_each_round_and_changes_nothing(self):
        plain = self.walked("", *self.SCRIPT)
        unchecked = self.attempts()
        self.fresh()
        linted = self.walked(check_table("lint", "implement", FAILING_LINT,
                                         "advisory"), *self.SCRIPT)

        self.assertEqual(linted[3], [("merged",)])
        self.assertEqual(linted, plain)
        attempts = self.attempts()
        self.assertEqual([row for row in attempts if row[0] != "lint"],
                         unchecked)
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
            self.assertIn("scripted-", detail["output"])
            self.assertNotIn("START", detail["output"])
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


    def test_a_trim_check_runs_once_on_the_head_trim_kept(self):
        self.configure("[trim]\nbudget_min = 1\n"
                       + check_table("tidy", "trim", ["echo tidy"], "advisory"))
        with patch.dict(os.environ, GIT_DATES):
            self.loop(TrimTurn(Commit("work", "work.txt", lines(60)),
                               Commit("tests", "test_work.py", WITNESS)),
                      TrimTurn(Commit("trim: delete", "work.txt", lines(40))),
                      APPROVE, Idle("nothing to retire"))

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        rows = self.read("SELECT stageId, inputSha, outputSha, outcome, edge,"
                         " json_extract(detail, '$.trim') FROM stageAttempts"
                         " ORDER BY id")
        stages = [row[0] for row in rows]
        self.assertEqual(stages.count("tidy"), 1)
        trim, tidy = rows[stages.index("tidy") - 1:stages.index("tidy") + 1]
        self.assertEqual((trim[0], trim[5]), ("trim", "kept"))
        self.assertNotEqual(trim[2], trim[1])
        self.assertEqual(tidy[1], trim[2])
        self.assertEqual(tidy[3:5], ("pass", "next"))

    def test_a_rotated_credential_runs_in_the_command_and_is_kept_from_the_store(self):
        config = self.db.parent / "config.toml"
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        marker = Path(held.name) / "said"
        self.configure(UNTRIMMED + '[extras]\napi_token = "old-credential-1"\n')
        rotated = (UNTRIMMED + '[extras]\napi_token = "rotated-credential-2"\n'
                   + check_table("say", "implement", [
                       f"echo rotated-credential-2 | tee {marker}"],
                       "advisory"))

        self.loop(EditsConfig(config, rotated), APPROVE,
                  Commit("second"), APPROVE,
                  provider=StubProvider(a_task(1), a_task(2)))

        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("merged",)] * 2)
        (say,) = [row for row in self.attempts(2) if row[0] == "say"]
        self.assertEqual(say[2], "pass")
        self.assertEqual(marker.read_text(), "rotated-credential-2\n")
        stored = self.read("SELECT document FROM pipelines") + self.read(
            "SELECT detail FROM stageAttempts")
        self.assertNotIn("rotated-credential-2", json.dumps(stored))

    def test_a_pinned_command_the_config_no_longer_holds_closes_error(self):
        config = self.db.parent / "config.toml"
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        markers = Path(held.name)
        self.configure(UNTRIMMED)

        def say(name):
            return UNTRIMMED + check_table(
                "say", "implement", [f"touch {markers / name}"], "advisory")

        self.loop(EditsConfig(config, say("pinned")), APPROVE,
                  EditsConfig(config, say("edited")), APPROVE,
                  provider=StubProvider(a_task(1), a_task(2)))

        (attempt,) = [row for row in self.attempts(2) if row[0] == "say"]
        self.assertEqual(attempt[2:4], ("error", "next"))
        self.assertIn("no longer holds", attempt[4]["error"])
        self.assertEqual(list(markers.iterdir()), [])

    def test_a_command_that_cannot_start_closes_error(self):
        self.walked(check_table("lint", "implement", ["no-such-linter-here"],
                                "advisory"), Commit("the scripted work"),
                    APPROVE)

        (lint,) = [row for row in self.attempts() if row[0] == "lint"]
        self.assertEqual(lint[2:4], ("error", "next"))
        self.assertEqual(lint[4]["exit_status"], 127)

    def test_an_abort_during_a_check_releases_the_ticket_on_the_board(self):
        abort = f"{sys.executable} -c " + shlex.quote(
            f"import sys, sqlite3; sys.path.insert(0, {str(HERE.parent)!r});"
            f" import store; store.abort(sqlite3.connect({str(self.db)!r}), 1,"
            " 'stop')")
        self.configure(UNTRIMMED + check_table("halt", "implement", [abort],
                                               "advisory"))

        self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("abandoned",)])
        self.assertIn(("unlabel", "iss-131"), [
            call[:2] for call in self.last_provider.label_calls])


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


class StaleRereviewCheckTests(unittest.TestCase):
    setUp = stale_fixture.StaleApprovalRereviewTests.setUp
    git = stale_fixture.StaleApprovalRereviewTests.git
    commit = stale_fixture.StaleApprovalRereviewTests.commit
    stale_reply = stale_fixture.StaleApprovalRereviewTests.stale_reply
    direct_reply = stale_fixture.StaleApprovalRereviewTests.direct_reply
    agents = stale_fixture.StaleApprovalRereviewTests.agents
    review_rounds = stale_fixture.StaleApprovalRereviewTests.review_rounds

    def test_a_stale_only_rereview_round_runs_its_check_again(self):
        config = self.project.config_path
        config.write_text(config.read_text()
                          + check_table("lint", "implement", ["true"],
                                        "advisory"))
        self.project = type(self.project).locate(self.root)

        sha, rnd, approved = self.review_rounds(self.stale_reply(),
                                                self.direct_reply())

        self.assertEqual((sha, rnd, approved), (self.head, 2, True))
        lints = self.conn.execute(
            "SELECT inputSha, outcome, json_extract(detail, '$.round')"
            " FROM stageAttempts WHERE stageId = 'lint' ORDER BY id").fetchall()
        self.assertEqual([tuple(row) for row in lints],
                         [(self.head, "pass", 1), (self.head, "pass", 2)])


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
