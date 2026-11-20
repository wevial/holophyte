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
import test_witness_turns as witness_fixture  # noqa: E402
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
    pinned_checks,
    pipeline_checks,
)
from holophyte.loop.stop import command  # noqa: E402
from holophyte.redact import REDACTED, values_held  # noqa: E402
from holophyte.stages import checks, record, standard  # noqa: E402

PausingCommit = witness_fixture.PausingCommit

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



class Forgets(Commit):
    def play(self, cwd, turn):
        record.PINNED_CHECKS.clear()
        return super().play(cwd, turn)


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

    def assert_checked_before_verify_and_review(self, attempts, check):
        for at, (stage, sha, _, _, detail) in enumerate(attempts):
            if stage in ("verify", "review"):
                earlier = [(row[1], row[4]["round"]) for row in attempts[:at]
                           if row[0] == check]
                self.assertIn((sha, detail["round"]), earlier, (stage, at))

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


    def test_one_config_read_supplies_every_pinned_check_and_its_secrets(self):
        self.configure(UNTRIMMED)
        lint = {"id": "lint", "commands": ["echo pinned-credential-1"],
                "mode": "advisory"}
        reads = iter([{"extras": {"api_token": "pinned-credential-1"},
                       "pipeline": {"check": [{**lint, "after": "implement"}]}}])
        edited = {"pipeline": {"check": [{**lint, "after": "trim"}]}}

        with patch("holophyte.config.pipeline_settings.load_config",
                   side_effect=lambda path: next(reads, edited)):
            raw, shown, _ = pinned_checks(self.project)

        self.assertEqual([(check["id"], check["after"]) for check in raw],
                         [("lint", "implement")])
        self.assertNotIn("pinned-credential-1", json.dumps(shown))


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
        lints = [row for row in attempts if row[0] == "lint"]
        self.assertEqual([row[4]["round"] for row in lints], [1, 2])
        for _, _, outcome, edge, detail in lints:
            self.assertEqual((outcome, edge), ("fail", "next"))
            self.assertEqual(detail["exit_status"], 1)
            self.assertRegex(detail["output"], r"(?m)^scripted-\d+\.txt$")
            self.assertNotIn("START", detail["output"])
        self.assert_checked_before_verify_and_review(attempts, "lint")

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

    def test_a_command_edited_after_the_pin_runs_as_pinned(self):
        config = self.db.parent / "config.toml"
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        marker = Path(held.name) / "said"

        def say(credential):
            return (UNTRIMMED + f'[extras]\napi_token = "{credential}"\n'
                    + check_table("say", "implement",
                                  [f"echo {credential} > {marker}"],
                                  "advisory"))

        self.configure(say("pinned-credential-1"))
        self.loop(EditsConfig(config, say("edited-credential-2")), APPROVE)

        (attempt,) = [row for row in self.attempts() if row[0] == "say"]
        self.assertEqual(attempt[2], "pass")
        self.assertEqual(marker.read_text(), "pinned-credential-1\n")

    def test_a_redacted_command_this_process_did_not_pin_closes_error(self):
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        marker = Path(held.name) / "said"

        self.walked('[extras]\napi_token = "forgotten-credential-3"\n'
                    + check_table("say", "implement", [
                        f"echo forgotten-credential-3 > {marker}"],
                        "advisory"), Forgets("the scripted work"), APPROVE)

        (attempt,) = [row for row in self.attempts() if row[0] == "say"]
        self.assertEqual(attempt[2:4], ("error", "next"))
        self.assertIn("did not pin", attempt[4]["error"])
        self.assertFalse(marker.exists())

    def test_a_credential_cut_by_the_runners_key_line_limit_leaves_no_fragment(self):
        secret = "cut-credential-0123456789"
        self.walked(f'[extras]\napi_token = "{secret}"\n' + check_table(
            "lint", "implement",
            [f"echo Error {'x' * 280}{secret}; exit 1"], "advisory"),
            Commit("the scripted work"), APPROVE)

        (lint,) = [row for row in self.attempts() if row[0] == "lint"]
        self.assertEqual(lint[2], "fail")
        self.assertIn("x" * 280, lint[4]["output"])
        for size in range(8, len(secret) + 1):
            self.assertNotIn(secret[:size], lint[4]["output"])
            self.assertNotIn(secret[-size:], lint[4]["output"])

    def test_a_check_that_edits_the_worktree_leaves_the_run_unchanged(self):
        plain = self.walked("", *self.SCRIPT)
        unchecked = self.attempts()
        self.fresh()
        edited = self.walked(check_table("regen", "implement", [
            "echo regenerated >> README.md", "touch generated.txt", "exit 1"],
            "advisory"), *self.SCRIPT)

        self.assertEqual(edited, plain)
        attempts = self.attempts()
        self.assertEqual([row for row in attempts if row[0] != "regen"],
                         unchecked)
        self.assertEqual([row[4]["restored"] for row in attempts
                          if row[0] == "regen"],
                         [["README.md", "generated.txt"]] * 2)
        self.assertEqual(self.git("show", "main:README.md"), "base\n")

    def test_a_check_that_only_narrows_a_tracked_files_mode_leaves_verify_passing(self):
        readable = ('[verify]\nalways = [\'[ -n "$(find README.md -perm'
                    ' -o=r)" ]\']\n')
        plain = self.walked(readable, *self.SCRIPT)
        self.fresh()
        narrowed = self.walked(readable + check_table(
            "lint", "implement", ["chmod 600 README.md", "exit 1"],
            "advisory"), *self.SCRIPT)

        self.assertEqual(narrowed, plain)
        self.assertEqual(narrowed[3], [("merged",)])
        lints = [row for row in self.attempts() if row[0] == "lint"]
        self.assertEqual([(row[2], row[3], row[4]["restored"])
                          for row in lints],
                         [("fail", "next", ["README.md"])] * 2)

    def test_a_glob_a_credential_would_redact_still_matches_its_paths(self):
        class DocsCommit(Commit):
            def play(self, cwd, turn):
                (cwd / "docs").mkdir(exist_ok=True)
                return super().play(cwd, turn)

        self.walked('[extras]\napi_token = "docs"\n' + check_table(
            "pages", "implement", ["true"], "advisory", ["docs/**"]),
            DocsCommit("the docs", "docs/guide.md"), APPROVE)

        (attempt,) = [row for row in self.attempts() if row[0] == "pages"]
        self.assertEqual(attempt[2:4], ("pass", "next"))

    def ignore_outputs(self):
        (self.target / ".gitignore").write_text("*.out\n")
        self.git("add", ".gitignore")
        with patch.dict(os.environ, GIT_DATES):
            self.git("commit", "-q", "-m", "ignore outputs")

    def test_a_check_leaves_the_index_dirty_untracked_and_ignored_files_as_found(self):
        self.ignore_outputs()
        (self.target / "README.md").write_text("base\ndirty\n")
        (self.target / "draft.txt").write_text("a draft\n")
        (self.target / "old.out").write_text("kept\n")
        status = self.git("status", "--porcelain", "--ignored")

        outcome, found = checks.ran(self.project, {
            "commands": ["echo more >> README.md", "git add -A",
                         "echo changed > old.out", "touch artifact.out",
                         "rm draft.txt", "exit 1"],
            "secrets": frozenset()}, self.target)

        self.assertEqual(outcome, "fail")
        self.assertEqual(self.git("status", "--porcelain", "--ignored"), status)
        self.assertEqual((self.target / "README.md").read_text(),
                         "base\ndirty\n")
        self.assertEqual((self.target / "draft.txt").read_text(), "a draft\n")
        self.assertEqual((self.target / "old.out").read_text(), "kept\n")
        self.assertFalse((self.target / "artifact.out").exists())
        self.assertEqual(found["restored"],
                         ["README.md", "artifact.out", "draft.txt", "old.out"])

    def test_a_check_that_deletes_an_ignored_artifact_leaves_verify_passing(self):
        class BuildsArtifact(Commit):
            def play(self, cwd, turn):
                (cwd / "build.out").write_text("built\n")
                return super().play(cwd, turn)

        self.ignore_outputs()
        script = (BuildsArtifact("the scripted work"), REQUEST_CHANGES,
                  BuildsArtifact("the scripted fix"), APPROVE)
        needs = "[verify]\nalways = ['test -f build.out']\n"
        plain = self.walked(needs, *script)
        self.fresh()
        self.ignore_outputs()
        cleaned = self.walked(needs + check_table(
            "clean", "implement", ["rm build.out", "exit 1"], "advisory"),
            *script)

        self.assertEqual(cleaned, plain)
        self.assertEqual(cleaned[3], [("merged",)])
        cleans = [row for row in self.attempts() if row[0] == "clean"]
        self.assertEqual([(row[2], row[3], row[4]["restored"])
                          for row in cleans],
                         [("fail", "next", ["build.out"])] * 2)

    def test_an_unreadable_staged_edit_is_put_back_with_the_index(self):
        status = self.git("status", "--porcelain")

        outcome, found = checks.ran(self.project, {
            "commands": ["echo edit >> README.md", "git add README.md",
                         "chmod 000 README.md", "exit 1"],
            "secrets": frozenset()}, self.target)

        self.assertEqual(outcome, "fail")
        self.assertEqual((self.target / "README.md").read_text(), "base\n")
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.assertEqual(found["restored"], ["README.md"])

    def test_a_file_a_check_hides_from_git_and_rewrites_is_put_back(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                outcome, found = checks.ran(self.project, {
                    "commands": [f"git update-index {flag} README.md",
                                 "echo hidden > README.md", "exit 1"],
                    "secrets": frozenset()}, self.target)

                self.assertEqual(outcome, "fail")
                self.assertEqual((self.target / "README.md").read_text(),
                                 "base\n")
                self.assertEqual(self.git("ls-files", "-v", "README.md"),
                                 "H README.md\n")
                self.assertEqual(found["restored"], ["README.md"])

    def test_a_tracked_file_deleted_before_a_check_stays_deleted(self):
        (self.target / "README.md").unlink()
        status = self.git("status", "--porcelain")

        outcome, found = checks.ran(self.project, {
            "commands": ["git checkout -- README.md", "exit 1"],
            "secrets": frozenset()}, self.target)

        self.assertEqual(outcome, "fail")
        self.assertFalse((self.target / "README.md").exists())
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.assertEqual(found["restored"], ["README.md"])

    def test_the_tail_is_the_commands_own_output_scrubbed(self):
        line = ('127.0.0.1 - - [20/Nov/2026:10:00:00 +0000]'
                ' "GET /health?token=access-credential-8 HTTP/1.1" 500 12')

        outcome, found = checks.ran(self.project, {
            "commands": [f"echo {shlex.quote(line)}", "exit 1"],
            "secrets": frozenset({"access-credential-8"})}, self.target)

        self.assertEqual((outcome, found["exit_status"]), ("fail", 1))
        self.assertEqual(found["output"], line.replace(
            "access-credential-8", REDACTED) + "\n")

    def test_a_snapshot_that_cannot_read_a_draft_closes_error_scrubbed(self):
        draft = self.target / "draft-snapshot-credential-7.txt"
        draft.write_text("a draft\n")
        draft.chmod(0)
        self.addCleanup(draft.chmod, 0o644)
        marker = self.target.parent / "ran.marker"

        outcome, found = checks.ran(self.project, {
            "commands": [f"touch {shlex.quote(str(marker))}"],
            "secrets": frozenset({"snapshot-credential-7"})}, self.target)

        self.assertEqual(outcome, "error")
        self.assertIn(REDACTED, found["error"])
        self.assertNotIn("credential-7", json.dumps(found))
        self.assertFalse(marker.exists())

    def test_a_check_puts_back_retargeted_symlinks_and_changed_modes(self):
        (self.target / "other.txt").write_text("other\n")
        self.git("add", "other.txt")
        self.git("commit", "-q", "-m", "another file")
        (self.target / "link").symlink_to("README.md")
        (self.target / "draft.sh").write_text("echo draft\n")
        (self.target / "draft.sh").chmod(0o644)

        outcome, found = checks.ran(self.project, {
            "commands": ["ln -sfn other.txt link", "chmod 755 draft.sh",
                         "exit 1"],
            "secrets": frozenset()}, self.target)

        self.assertEqual(outcome, "fail")
        self.assertEqual(os.readlink(self.target / "link"), "README.md")
        self.assertEqual((self.target / "other.txt").read_text(), "other\n")
        self.assertEqual((self.target / "draft.sh").stat().st_mode & 0o777,
                         0o644)
        self.assertEqual(found["restored"], ["draft.sh", "link"])

    def test_a_read_only_staged_draft_is_put_back_with_the_index(self):
        (self.target / "draft.txt").write_text("a draft\n")
        (self.target / "draft.txt").chmod(0o644)
        status = self.git("status", "--porcelain")

        outcome, found = checks.ran(self.project, {
            "commands": ["echo edit >> draft.txt", "chmod 444 draft.txt",
                         "git add draft.txt", "exit 1"],
            "secrets": frozenset()}, self.target)

        self.assertEqual(outcome, "fail")
        self.assertEqual((self.target / "draft.txt").read_text(), "a draft\n")
        self.assertEqual((self.target / "draft.txt").stat().st_mode & 0o777,
                         0o644)
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.assertEqual(found["restored"], ["draft.txt"])

    def test_a_path_that_cannot_be_put_back_still_restores_the_index(self):
        (self.target / "draft.txt").write_text("a draft\n")

        outcome, found = checks.ran(self.project, {
            "commands": ["git add draft.txt", "rm draft.txt",
                         "mkdir -p draft.txt/inner", "touch draft.txt/inner/x",
                         "exit 1"],
            "secrets": frozenset()}, self.target)

        self.assertEqual((outcome, found["check_outcome"]), ("error", "fail"))
        self.assertIn("draft.txt", found["restore_error"])
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_a_failed_restore_closes_error_and_keeps_the_checks_result(self):
        with patch.object(checks.Snapshot, "restore",
                          side_effect=OSError("restore refused")):
            outcome, found = checks.ran(self.project, {
                "commands": ["echo clean"], "secrets": frozenset()},
                self.target)

        self.assertEqual(outcome, "error")
        self.assertEqual((found["check_outcome"], found["exit_status"]),
                         ("pass", 0))
        self.assertIn("clean", found["output"])
        self.assertIn("restore refused", found["restore_error"])

    def test_a_registered_value_in_a_selector_neither_runs_nor_drops_a_check(self):
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        markers = Path(held.name)
        with values_held(["off", "implement"]):
            self.walked(check_table("dormant", "implement",
                                    [f"touch {markers / 'dormant'}"], "off")
                        + check_table("lint", "implement",
                                      [f"touch {markers / 'lint'}"],
                                      "advisory"),
                        Commit("the scripted work"), APPROVE)

        found = {row[0]: row for row in self.attempts()}
        self.assertEqual(found["dormant"][2], "skipped")
        self.assertEqual(found["lint"][2:4], ("pass", "next"))
        self.assertEqual(sorted(path.name for path in markers.iterdir()),
                         ["lint"])

    def test_a_literal_redaction_marker_runs_without_the_pinning_process(self):
        self.walked(check_table("say", "implement", ["echo '[redacted]'"],
                                "advisory"), Forgets("the scripted work"),
                    APPROVE)

        (attempt,) = [row for row in self.attempts() if row[0] == "say"]
        self.assertEqual(attempt[2:4], ("pass", "next"))

    def test_a_resumed_run_runs_the_command_its_predecessor_pinned(self):
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        marker = Path(held.name) / "said"
        self.configure(UNTRIMMED + '[extras]\napi_token = "resumed-credential-4"\n'
                       + check_table("say", "implement", [
                           f"echo resumed-credential-4 > {marker}"],
                           "advisory"))
        self.loop(PausingCommit(self.db, Commit("the scripted work")))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("paused",)])

        command(self.project, "KO-131", None, resume=True)
        self.loop(APPROVE)

        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("paused",), ("merged",)])
        (attempt,) = [row for row in self.attempts(2) if row[0] == "say"]
        self.assertEqual(attempt[2], "pass")
        self.assertEqual(marker.read_text(), "resumed-credential-4\n")

    def test_a_rotated_glob_and_restored_path_are_scrubbed_from_the_store(self):
        config = self.db.parent / "config.toml"
        self.configure(UNTRIMMED)
        rotated = (UNTRIMMED + '[extras]\napi_token = "glob-credential-5"\n'
                   + check_table("paths", "implement", ["true"], "advisory",
                                 ["glob-credential-5/**"])
                   + check_table("report", "implement", [
                       "touch report-glob-credential-5.txt", "exit 1"],
                       "advisory"))

        self.loop(EditsConfig(config, rotated), APPROVE,
                  Commit("second"), APPROVE,
                  provider=StubProvider(a_task(1), a_task(2)))

        found = {row[0]: row for row in self.attempts(2)}
        self.assertEqual(found["paths"][2], "skipped")
        self.assertEqual(found["report"][2], "fail")
        self.assertEqual(len(found["report"][4]["restored"]), 1)
        stored = json.dumps(self.read("SELECT detail FROM stageAttempts")
                            + self.read("SELECT payload FROM runEvents"))
        self.assertNotIn("glob-credential-5", stored)

    def test_a_credential_only_the_pin_saw_is_scrubbed_after_a_rotation(self):
        config = self.db.parent / "config.toml"
        self.configure(UNTRIMMED + '[extras]\napi_token = "loaded-credential-a"\n')

        def with_token(token, check=""):
            return UNTRIMMED + f'[extras]\napi_token = "{token}"\n' + check

        pinned = with_token("pinned-credential-b", check_table(
            "say", "implement", ["echo pinned-credential-b"], "advisory"))
        self.loop(EditsConfig(config, pinned), APPROVE,
                  EditsConfig(config, with_token("rotated-credential-c")),
                  APPROVE, provider=StubProvider(a_task(1), a_task(2)))

        (attempt,) = [row for row in self.attempts(2) if row[0] == "say"]
        self.assertEqual(attempt[2], "pass")
        self.assertIn(REDACTED, attempt[4]["output"])
        self.assertNotIn("pinned-credential-b",
                         json.dumps(self.read("SELECT detail FROM stageAttempts")))

    def test_a_rejected_glob_is_not_repeated_in_the_refusal(self):
        self.configure(check_table("first", "trim", ["true"], "advisory",
                                   ["/var/held-credential-6/*"]))

        with self.assertRaises(SystemExit) as stopped:
            check_config(self.project)

        self.assertIn("[[pipeline.check]] first: paths", str(stopped.exception))
        self.assertNotIn("held-credential-6", str(stopped.exception))

    def test_a_command_that_exits_127_after_starting_closes_fail(self):
        self.walked(check_table("lint", "implement",
                                ["echo test fixture not found; exit 127"],
                                "advisory"),
                    Commit("the scripted work"), APPROVE)

        (lint,) = [row for row in self.attempts() if row[0] == "lint"]
        self.assertEqual(lint[2:4], ("fail", "next"))
        self.assertEqual(lint[4]["exit_status"], 127)

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


    def test_a_redacted_check_without_its_original_still_skips(self):
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        markers = Path(held.name)

        def touch(name):
            return [f"touch {markers / name} skipped-credential-9"]

        self.walked('[extras]\napi_token = "skipped-credential-9"\n'
                    + check_table("docs", "implement", touch("docs"),
                                  "advisory", ["docs/**"])
                    + check_table("dormant", "implement", touch("dormant"),
                                  "off"),
                    Forgets("the scripted work"), APPROVE)

        found = {row[0]: row for row in self.attempts()}
        self.assertEqual(found["docs"][2], "skipped")
        self.assertIn("docs/**", found["docs"][4]["reason"])
        self.assertEqual(found["dormant"][2], "skipped")
        self.assertEqual(found["dormant"][4], {"reason": "mode off"})
        self.assertEqual(list(markers.iterdir()), [])

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


class WitnessRepairCheckTests(witness_fixture.RunningRepo):
    attempts = CheckRuns.attempts
    assert_checked_before_verify_and_review = (
        CheckRuns.assert_checked_before_verify_and_review)

    def test_a_witness_repair_is_checked_before_its_reverify(self):
        self.configure(UNTRIMMED + check_table("lint", "implement", ["true"],
                                               "advisory"))

        self.run_task(witness_fixture.gated(),
                      Commit("run the gated test", path="tests/test_gate.py",
                             body=witness_fixture.OPENED), APPROVE)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        attempts = self.attempts()
        lints = [row[1] for row in attempts if row[0] == "lint"]
        self.assertEqual(len(set(lints)), 2)
        self.assert_checked_before_verify_and_review(attempts, "lint")


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
