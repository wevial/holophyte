"""Reviewer replies in the output schema's JSON: the Codex reviewer argv and
the review container's command name the schema, a schema-valid reply renders
to the line format every reader parses, and any other reply is parsed as it
always was."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store  # noqa: E402
from holophyte.agents import probes, roles  # noqa: E402
from holophyte.agents.agent_output import AgentOutput  # noqa: E402
from holophyte.agents.agent_routes import reset  # noqa: E402
from holophyte.agents.harness import ADAPTERS, Seat  # noqa: E402
from holophyte.agents.review_workspace import configured_review  # noqa: E402
from holophyte.babysit.description_review import (  # noqa: E402
    description_findings,
    without_description,
)
from holophyte.loop.gates import sh  # noqa: E402
from holophyte.review import review_runner  # noqa: E402
from holophyte.review.json_reply import (  # noqa: E402
    REPLY_BRIEF,
    SCHEMA_FILE,
    read_reply,
    reply_brief,
)
from holophyte.review.reply_parsing import (  # noqa: E402
    FINDING_CATEGORIES,
    _review_reply,
    criteria_findings,
    criterion_accounts,
    follow_up_lines,
    parse_findings,
)
from holophyte.review.review_runner import terminal_verdict  # noqa: E402
from tests.sweep_fixture import SweepTestCase  # noqa: E402
from tests.test_capacity_fallback import AT_CAPACITY  # noqa: E402
from tests.test_review_runner import (  # noqa: E402
    IMAGE_SHIM,
    docker_shim,
    two_commit_repo,
)

REPLY = {
    "verdict": "REQUEST_CHANGES",
    "criteria": [
        {"id": "C1", "status": "met", "reason": "the parser test covers it",
         "witness": "tests/test_app.py::LoadTests::test_empty_input"},
        {"id": "C2", "status": "not_met",
         "reason": "load() still crashes on a blank line", "witness": None},
    ],
    "findings": [
        {"path": "src/app.py", "line": 12, "severity": "p1",
         "category": "correctness", "target": "code",
         "message": "load() indexes the first row\nof an empty list."},
        {"path": None, "line": None, "severity": "p2", "category": "docs",
         "target": "description",
         "message": "The summary promises a retry the diff does not add."},
    ],
    "follow_ups": [
        {"kind": "guardrail", "text": "Pin the blank-line case",
         "path": "tests/test_app.py", "line": 40},
    ],
    "scope": [],
    "refuted": [],
}

CODEX_TRANSCRIPT = ("session id: 0199\n--------\nuser\nReview the candidate.\n"
                    "codex\nReading src/app.py.\ntokens used\n1,234\n")

MARKDOWN = ("- src/app.py:3 [p1] [correctness] load() crashes on empty input.\n"
            "CRITERION C1: met — tests/test_app.py::LoadTests::test_empty_input\n"
            "VERDICT: REQUEST_CHANGES")


def reviewer_seat(role="reviewer"):
    return Seat(ADAPTERS["codex"], "codex", {"model": "gpt-5", "effort": "low"},
                role)


def named_schema(argv):
    return json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())


def objects(rule):
    if rule.get("type") == "object":
        yield rule
        for field in rule["properties"].values():
            yield from objects(field)
    if "items" in rule:
        yield from objects(rule["items"])


class ReviewerArgvTests(unittest.TestCase):
    def test_a_reviewer_turn_and_resume_name_a_schema_with_the_reply_fields(self):
        seat = reviewer_seat()
        for argv in (seat.turn("review the candidate"), seat.resume("0199")):
            with self.subTest(argv=argv[:3]):
                schema = named_schema(argv)
                fields = schema["properties"]
                self.assertLessEqual(
                    {"verdict", "criteria", "findings", "follow_ups"},
                    set(schema["required"]))
                self.assertEqual(fields["verdict"]["enum"],
                                 ["APPROVE", "REQUEST_CHANGES"])
                criterion = fields["criteria"]["items"]
                self.assertLessEqual({"id", "status", "reason", "witness"},
                                     set(criterion["required"]))
                self.assertEqual(criterion["properties"]["status"]["enum"],
                                 ["met", "not_met", "unwitnessed"])
                finding = fields["findings"]["items"]
                self.assertLessEqual(
                    {"path", "line", "severity", "category", "target", "message"},
                    set(finding["required"]))
                self.assertEqual(finding["properties"]["severity"]["enum"],
                                 ["p0", "p1", "p2"])
                self.assertEqual(finding["properties"]["category"]["enum"],
                                 list(FINDING_CATEGORIES))
                self.assertEqual(finding["properties"]["target"]["enum"],
                                 ["code", "description"])
                follow_up = fields["follow_ups"]["items"]
                self.assertLessEqual({"kind", "text", "path", "line"},
                                     set(follow_up["required"]))
                self.assertEqual(follow_up["properties"]["kind"]["enum"],
                                 ["feature", "guardrail"])

    def test_every_schema_object_is_closed_and_requires_each_property(self):
        for rule in objects(named_schema(reviewer_seat().turn("review"))):
            with self.subTest(fields=sorted(rule["properties"])):
                self.assertIs(rule["additionalProperties"], False)
                self.assertEqual(sorted(rule["required"]),
                                 sorted(rule["properties"]))

    def test_an_adjudicator_turn_carries_no_reply_schema(self):
        seat = reviewer_seat("adjudicator")
        for argv in (seat.turn("adjudicate"), seat.resume("0199")):
            self.assertNotIn("--output-schema", argv)


class ContainerReviewTests(unittest.TestCase):
    """The container's exec line runs in a real shell, its /home/reviewer
    read through the host directory the command mounts there, with each CLI
    replaced by a stub that prints the arguments it was given."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ("candidate", "home", "toolchain", "bin"):
            (self.root / name).mkdir()

    def exec_argv(self, harness, reply_schema):
        command = review_runner.container_command(
            image="holophyte-reviewer:test", workspace=self.root / "candidate",
            reviewer_home=self.root / "home", toolchain=self.root / "toolchain",
            name="holophyte-review-test", prompt="review the candidate",
            uid=1000, gid=1000, harness=harness, credential="TOKEN",
            reply_schema=reply_schema)
        [home] = [mount.split(":")[0] for mount in
                  (command[i + 1] for i, arg in enumerate(command)
                   if arg == "--volume")
                  if mount.split(":")[1] == "/home/reviewer"]
        stub = self.root / "bin" / "cli"
        stub.write_text('#!/bin/sh\nfor arg in "$@"; do printf "%s\\0" "$arg"; '
                        'done\n')
        stub.chmod(0o755)
        script = command[command.index("-c") + 1]
        run = script[script.index("exec /opt/"):].replace(
            "/home/reviewer/", f"{home}/").replace(
            f"/opt/{harness}/bin/{harness}", str(stub))
        positional = command[command.index(script) + 1:]
        out = subprocess.run(["sh", "-eu", "-c", run, *positional],
                             capture_output=True, text=True, check=True,
                             cwd=self.root, env=dict(os.environ, PATH="/usr/bin:/bin"))
        return out.stdout.split("\0")[:-1], Path(home)

    def test_a_codex_container_review_names_the_schema_staged_in_its_home(self):
        argv, home = self.exec_argv("codex", True)
        named = Path(argv[argv.index("--output-schema") + 1])
        self.assertEqual(named.parent, home)
        self.assertEqual(json.loads(named.read_text()),
                         json.loads(SCHEMA_FILE.read_text()))
        self.assertEqual(argv[-1], "review the candidate")

    def test_a_claude_container_review_passes_the_staged_schema_text(self):
        argv, _ = self.exec_argv("claude", True)
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]),
                         json.loads(SCHEMA_FILE.read_text()))
        self.assertEqual(argv[-1], "review the candidate")

    def test_a_container_turn_without_the_schema_stages_and_names_none(self):
        for harness, flag in (("codex", "--output-schema"),
                              ("claude", "--json-schema")):
            with self.subTest(harness=harness):
                argv, home = self.exec_argv(harness, False)
                self.assertEqual(list(home.iterdir()), [])
                self.assertNotIn(flag, argv)

    def test_the_standalone_runner_starts_its_review_with_the_schema(self):
        bin_dir, env = docker_shim(self.root / "shim")
        (bin_dir / "docker").write_text(IMAGE_SHIM)
        env["HOLOPHYTE_DOCKER_IMAGES"] = str(self.root / "docker.images")
        (self.root / "docker.images").write_text(f"{review_runner.IMAGE}\n")
        (self.root / "auth.json").write_text("{}")
        (self.root / "prompt.txt").write_text("review")
        base, candidate = two_commit_repo(self.root / "repo")
        argv = ["review_runner", "--repo", str(self.root / "repo"),
                "--base", base, "--candidate", candidate,
                "--prompt-file", str(self.root / "prompt.txt")]
        with patch.dict(os.environ, env), patch.object(sys, "argv", argv), \
                patch.object(review_runner, "SCRATCH_ROOT", self.root / "reviews"), \
                patch.object(review_runner, "CODEX_AUTH", self.root / "auth.json"), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(review_runner.main(), 0)
        self.assertEqual(printed.getvalue(), "VERDICT: APPROVE\n")
        log = Path(env["HOLOPHYTE_DOCKER_LOG"]).read_text()
        self.assertIn(f"--output-schema {review_runner.REPLY_SCHEMA} ", log)

    def test_a_claude_structured_reply_is_read_as_the_json_reply(self):
        output = json.dumps({"is_error": False, "result": "",
                             "structured_output": REPLY})
        message, _ = review_runner.parse_claude_output(output, None)
        text, reply_format = read_reply(message)
        self.assertEqual(reply_format, "json")
        self.assertTrue(text.endswith("VERDICT: REQUEST_CHANGES"))

    def test_a_claude_markdown_result_outlives_an_off_schema_structured_one(self):
        output = json.dumps({"is_error": False, "result": MARKDOWN,
                             "structured_output": {"verdict": "APPROVE"}})
        message, verdict = review_runner.parse_claude_output(output)
        self.assertEqual((message, verdict), (MARKDOWN, "REQUEST_CHANGES"))
        self.assertEqual(read_reply(message), (MARKDOWN, "markdown"))

    def test_the_default_container_route_gets_the_field_brief(self):
        self.assertEqual(reply_brief(brief_project(self, {}), "LINES"),
                         REPLY_BRIEF)
        self.assertEqual(reply_brief(brief_project(
            self, {"reviewer": "reviewer --lines"}), "LINES"), "LINES")


def brief_project(case, agents):
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    return SimpleNamespace(path=root, holo_dir=root,
                           config=lambda: {"agents": agents},
                           config_path=root / "config.toml")


class ConfiguredCommandTests(unittest.TestCase):
    """The configured command runs as a real process: a `codex` stub that
    prints the arguments it was launched with."""

    COMMAND = "codex exec -m gpt-6-astra"

    def launched(self, role, command):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stub = Path(tmp.name) / shlex.split(command)[0]
        stub.write_text('#!/bin/sh\nfor arg in "$@"; do printf "%s\\0" "$arg"; '
                        'done\n')
        stub.chmod(0o755)
        argv = [str(stub), *shlex.split(command)[1:], "review the candidate"]
        output = configured_review(roles.review_argv(role == "review", argv),
                                   tmp.name, 30,
                                   dict(os.environ), role, command)
        return str(output).split("\0")[:-1]

    def test_a_configured_codex_review_command_requires_the_reply_schema(self):
        for command in (self.COMMAND,
                        "codex -c model_reasoning_effort=low exec -m gpt-6-astra",
                        "codex exec --output-schema other.json -m gpt-6-astra",
                        "codex exec --output-schema=other.json -m gpt-6-astra"):
            with self.subTest(command=command):
                argv = self.launched("review", command)
                self.assertEqual(argv.count("--output-schema"), 1)
                self.assertEqual(named_schema(argv),
                                 json.loads(SCHEMA_FILE.read_text()))
                self.assertEqual(argv[-3:],
                                 ["-m", "gpt-6-astra", "review the candidate"])
                self.assertEqual(reply_brief(brief_project(
                    self, {"reviewer": command}), "LINES"), REPLY_BRIEF)

    def test_a_non_codex_review_command_launches_as_configured(self):
        self.assertEqual(self.launched("review", "wrapper exec --review"),
                         ["exec", "--review", "review the candidate"])

    def test_a_non_codex_command_naming_its_own_schema_gets_the_line_brief(self):
        command = "wrapper exec --output-schema wrapper.json"
        self.assertEqual(self.launched("review", command),
                         ["exec", "--output-schema", "wrapper.json",
                          "review the candidate"])
        self.assertEqual(reply_brief(brief_project(
            self, {"reviewer": command}), "LINES"), "LINES")


class DispatchedBriefTests(SweepTestCase):
    """Turns go through the real dispatcher: the container launch is
    replaced, and a fallback reviewer is a real script that records the
    prompt it was handed."""

    def setUp(self):
        super().setUp()
        sh(["git", "init", "-q", str(self.target)])
        sh(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
            "commit", "--allow-empty", "-qm", "base"], cwd=self.target)
        self.sha = sh(["git", "rev-parse", "HEAD"], cwd=self.target)
        self.addCleanup(reset, self.project)
        self.prompts = self.root / "prompts"
        self.wrapper = self.root / "wrapper"
        self.wrapper.write_text(
            f"#!{sys.executable}\nimport subprocess, sys\n"
            f"with open({str(self.prompts)!r}, 'a') as f:\n"
            " f.write(repr(sys.argv[-1]) + '\\n')\n"
            f"if sys.argv[-1] == {probes.REVIEW_PROBE_GOAL!r}:\n"
            " print('ready', subprocess.check_output(\n"
            "     ['git', 'rev-parse', 'HEAD'], text=True).strip())\n"
            "else:\n print('VERDICT: APPROVE')\n")
        self.wrapper.chmod(0o755)

    def sent(self):
        return [line for line in map(eval, self.prompts.read_text().splitlines())
                if line != probes.REVIEW_PROBE_GOAL]

    def dispatch(self, goal, container, **turn):
        with patch.object(roles.review_runner, "run_review",
                          side_effect=container), \
                contextlib.redirect_stdout(io.StringIO()):
            return roles.agent(self.project, "review", goal, self.target,
                               base_sha=self.sha, candidate_sha=self.sha,
                               conn=self.conn, run_id=self.a_run(), **turn)

    def test_a_markdown_fallback_after_a_capacity_failure_gets_the_line_brief(self):
        self.configure(f'[agents]\nreviewer_fallback = ["{self.wrapper}"]\n')
        script = self.root / "container"
        script.write_text(AT_CAPACITY)
        launched = []

        def at_capacity(*, candidate_sha, prompt, reply_schema=False, **_):
            launched.append((prompt, reply_schema))
            review_runner._run([sys.executable, str(script), candidate_sha,
                                prompt])
        goal = "Review.\n" + reply_brief(self.project, "LINES")
        self.assertEqual(self.dispatch(goal, at_capacity), "VERDICT: APPROVE")
        self.assertEqual(launched, [(f"Review.\n{REPLY_BRIEF}", True)])
        self.assertEqual(self.sent(), ["Review.\nLINES"])

    def recording_cli(self, name, reply):
        cli = self.root / f"{name}-bin" / name
        cli.parent.mkdir()
        cli.write_text(
            f"#!{sys.executable}\nimport json, subprocess, sys\n"
            f"with open({str(self.root / name)!r}, 'a') as f:\n"
            " f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"if sys.argv[-1] == {probes.REVIEW_PROBE_GOAL!r}:\n"
            " print('ready', subprocess.check_output(\n"
            "     ['git', 'rev-parse', 'HEAD'], text=True).strip())\n"
            f"else:\n print({reply!r})\n")
        cli.chmod(0o755)
        return cli

    def received(self, name):
        return [argv for argv in map(json.loads, (
            self.root / name).read_text().splitlines())
                if argv[-1] != probes.REVIEW_PROBE_GOAL]

    def test_a_codex_fallback_after_a_markdown_route_gets_the_schema(self):
        claude = self.recording_cli("claude", json.dumps(
            {"type": "result", "is_error": True,
             "result": "You've hit your limit \u00b7 resets tomorrow"}))
        codex = self.recording_cli("codex", "VERDICT: APPROVE")
        self.configure(f'[agents]\nreviewer = "{claude} -p --output-format json"\n'
                       f'reviewer_fallback = "{codex} exec -m gpt-6-astra"\n')
        goal = "Review.\n" + reply_brief(self.project, "LINES")
        with contextlib.redirect_stdout(io.StringIO()):
            roles.agent(self.project, "review", goal, self.target,
                        base_sha=self.sha, candidate_sha=self.sha,
                        conn=self.conn, run_id=self.a_run())
        [[*_, sent]] = self.received("claude")
        self.assertEqual(sent, "Review.\nLINES")
        [argv] = self.received("codex")
        self.assertEqual(named_schema(argv), json.loads(SCHEMA_FILE.read_text()))
        self.assertEqual(argv[-1], f"Review.\n{REPLY_BRIEF}")

    def test_a_refusal_fallback_command_gets_the_line_brief(self):
        goal = "Review.\n" + reply_brief(self.project, "LINES")
        roles._agent(self.project, "review", goal, self.target,
                     base_sha=self.sha, candidate_sha=self.sha,
                     fallback_command=str(self.wrapper))
        self.assertEqual(self.sent(), ["Review.\nLINES"])

    def test_a_brief_asking_for_lines_starts_a_container_without_the_schema(self):
        launched = []

        def container(*, prompt, reply_schema=False, **_):
            launched.append((prompt, reply_schema))
            return "VERDICT: APPROVE"
        self.dispatch("Review. End with VERDICT: APPROVE.", container)
        self.assertEqual(launched, [("Review. End with VERDICT: APPROVE.",
                                     False)])


class RenderedReplyTests(unittest.TestCase):
    def setUp(self):
        self.text, self.format = read_reply(CODEX_TRANSCRIPT + json.dumps(REPLY))

    def test_findings_parse_one_line_each_with_their_paths(self):
        self.assertEqual(self.format, "json")
        code, described = parse_findings(self.text)
        self.assertEqual((code["path"], code["line"], code["severity"],
                          code["category"]), ("src/app.py", 12, "p1", "correctness"))
        self.assertIn("first row of an empty list", code["message"])
        self.assertNotIn("\n", code["message"])
        self.assertTrue(described["path"].startswith("(unparsed):"))
        self.assertIn('path: "PR description"', described["message"])
        self.assertIn("retry the diff does not add", described["message"])

    def test_without_description_takes_only_the_description_finding(self):
        kept, described = without_description(self.text)
        self.assertEqual(described, ['- path: "PR description" [p2] The summary'
                                     ' promises a retry the diff does not add.'])
        self.assertEqual([finding["path"] for finding in parse_findings(kept)],
                         ["src/app.py"])

    def test_a_linked_citation_in_a_message_keeps_the_finding_location(self):
        finding = dict(REPLY["findings"][0], message=(
            "see [the regression test](tests/test_app.py:40)"))
        text, _ = read_reply(json.dumps(dict(REPLY, findings=[finding])))
        [parsed] = parse_findings(text)
        self.assertEqual((parsed["path"], parsed["line"]), ("src/app.py", 12))
        self.assertIn("tests/test_app.py:40", parsed["message"])

    def test_code_in_backticks_is_not_read_as_a_link(self):
        finding = dict(REPLY["findings"][0],
                       message="load() calls\n`handlers[0](event)` on an empty list")
        text, _ = read_reply(json.dumps(dict(REPLY, findings=[finding])))
        [parsed] = parse_findings(text)
        self.assertEqual((parsed["path"], parsed["line"]), ("src/app.py", 12))
        self.assertIn("`handlers[0](event)` on an empty", parsed["message"])

    def test_a_candidate_absolute_path_keeps_its_location(self):
        finding = dict(REPLY["findings"][0],
                       path="/home/reviewer/candidate/src/app.py",
                       message="load() calls\n`handlers[0](event)` on an empty list")
        text, _ = read_reply(json.dumps(dict(REPLY, findings=[finding])))
        [parsed] = parse_findings(text)
        self.assertEqual((parsed["path"], parsed["line"]), ("src/app.py", 12))

    def test_a_candidate_absolute_follow_up_path_is_cited_from_the_root(self):
        follow_up = dict(REPLY["follow_ups"][0],
                         path="/workspace/tests/test_app.py")
        text, _ = read_reply(json.dumps(dict(REPLY, follow_ups=[follow_up])))
        self.assertEqual(follow_up_lines(text),
                         ["FOLLOW_UP(guardrail): Pin the blank-line case"
                          " @ tests/test_app.py:40"])

    def test_a_message_keeps_the_spacing_inside_its_quoted_code(self):
        finding = dict(REPLY["findings"][0],
                       message='load() splits on\n`"user  id"` and loses a column')
        text, _ = read_reply(json.dumps(dict(REPLY, findings=[finding])))
        [parsed] = parse_findings(text)
        self.assertIn('splits on `"user  id"` and', parsed["message"])

    def test_a_location_the_line_format_cannot_carry_is_not_moved(self):
        for path, line in (("docs/Release Notes.md", 12),
                           ("app/[id]/page.tsx", 12), ("LICENSE", 12),
                           ("docker/Makefile", None)):
            with self.subTest(path=path):
                finding = dict(REPLY["findings"][0], path=path, line=line,
                               message="see docs/development.md:9")
                text, _ = read_reply(json.dumps(dict(REPLY, findings=[finding])))
                [parsed] = parse_findings(text)
                self.assertTrue(parsed["path"].startswith("(unparsed):"))
                where = path if line is None else f"{path}:{line}"
                self.assertIn(f"(at {where})", parsed["message"])
                self.assertEqual(parsed["category"], "correctness")

    def test_a_message_citation_never_becomes_a_finding_location(self):
        cited = "see docs/development.md:272"
        for label, finding in (
                ("description", dict(REPLY["findings"][1], message=cited)),
                ("no path", dict(REPLY["findings"][0], path=None, line=None,
                                 message=cited))):
            with self.subTest(finding=label):
                text, _ = read_reply(json.dumps(dict(REPLY, findings=[finding])))
                [parsed] = parse_findings(text)
                self.assertTrue(parsed["path"].startswith("(unparsed):"))
                self.assertIn(cited, parsed["message"])

    def test_a_pathless_code_finding_is_not_taken_for_a_description_one(self):
        finding = dict(REPLY["findings"][0], path=None, line=None,
                       message='path: "PR description" is quoted by load()')
        text, _ = read_reply(json.dumps(dict(REPLY, findings=[finding])))
        self.assertEqual(without_description(text)[1], [])

    def test_witness_names_and_scope_paths_keep_their_inner_spaces(self):
        witness = 'console/view.test.ts::"shows  both lines"'
        criterion = dict(REPLY["criteria"][0], witness=witness)
        entry = {"path": "docs/Release  Notes.md", "status": "needed",
                 "reason": "the guide\nnames it"}
        text, _ = read_reply(json.dumps(dict(REPLY, criteria=[criterion],
                                             scope=[entry])))
        [account] = criterion_accounts(text)
        self.assertTrue(account["note"].endswith(witness))
        self.assertEqual(criteria_findings(text, (), scope=(entry["path"],)), [])

    def test_a_witness_cited_in_the_reason_keeps_its_inner_spaces(self):
        witness = 'console/view.test.ts::"shows  both lines"'
        criterion = dict(REPLY["criteria"][0], reason=f"covered by {witness}",
                         witness=witness)
        text, _ = read_reply(json.dumps(dict(REPLY, criteria=[criterion])))
        [account] = criterion_accounts(text)
        self.assertEqual(account["note"].count(witness), 2)

    def test_a_met_criterion_naming_nothing_is_unwitnessed(self):
        criterion = dict(REPLY["criteria"][0], reason=" ", witness=None)
        reply = dict(REPLY, verdict="APPROVE", findings=[],
                     criteria=[criterion, REPLY["criteria"][1]])
        text, _ = read_reply(json.dumps(reply))
        self.assertEqual([(match["n"], match["status"])
                          for match in criterion_accounts(text)],
                         [("C1", "unwitnessed"), ("C2", "not met")])
        self.assertEqual([finding["line"] for finding in criteria_findings(
            text, ("loads", "skips blanks"))], [1, 2])

    def test_a_lone_finding_parses_as_its_markdown_twin_does(self):
        reply = dict(REPLY, criteria=[], follow_ups=[],
                     findings=[REPLY["findings"][0]])
        text, _ = read_reply(json.dumps(reply))
        twin = ("- src/app.py:12 [p1] [correctness] load() indexes the first"
                " row of an empty list.\n\nVERDICT: REQUEST_CHANGES")
        self.assertEqual(parse_findings(text), parse_findings(twin))

    def test_refuted_findings_are_kept_as_notes_not_blockers(self):
        refuted = [{"finding": "src/app.py:30 leaks a handle",
                    "reason": "the with block closes it"}]
        text, _ = read_reply(json.dumps(dict(REPLY, refuted=refuted)))
        self.assertIn("the with block closes it", text)
        self.assertEqual([finding["path"] for finding in parse_findings(text)],
                         [finding["path"] for finding in parse_findings(self.text)])

    def test_a_markdown_follow_up_still_keeps_the_round_off_the_rewrite(self):
        answer = ('- path: "PR description" [p2] The summary promises a retry.\n'
                  "FOLLOW_UP(guardrail): Pin the blank-line case\n"
                  "VERDICT: REQUEST_CHANGES")
        text, reply_format = read_reply(answer)
        self.assertEqual(reply_format, "markdown")
        self.assertEqual(description_findings(text, "REQUEST_CHANGES", []), [])

    def test_criteria_follow_ups_and_verdict_read_from_the_rendering(self):
        self.assertEqual([(match["n"], match["status"])
                          for match in criterion_accounts(self.text)],
                         [("C1", "met"), ("C2", "not met")])
        self.assertEqual(follow_up_lines(self.text),
                         ["FOLLOW_UP(guardrail): Pin the blank-line case"
                          " @ tests/test_app.py:40"])
        self.assertEqual(terminal_verdict(self.text), "REQUEST_CHANGES")

    def test_a_criterion_id_in_other_words_is_still_accounted_by_number(self):
        criterion = dict(REPLY["criteria"][1], id="Criterion 2")
        text, _ = read_reply(json.dumps(dict(REPLY, criteria=[criterion])))
        self.assertEqual([(match["n"], match["status"])
                          for match in criterion_accounts(text)],
                         [("C2", "not met")])

    def test_a_scope_entry_answers_the_scope_question(self):
        for status, blocked in (("needed", []), ("tangent", ["docs/extra.md"])):
            with self.subTest(status=status):
                entry = {"path": "docs/extra.md", "status": status,
                         "reason": "the guide names the new flag"}
                text, _ = read_reply(json.dumps(dict(REPLY, scope=[entry])))
                self.assertEqual(
                    [finding["path"] for finding in criteria_findings(
                        text, (), scope=("docs/extra.md",))], blocked)


class ReplyFormatTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(Path(tmp.name) / "store.db")
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.ensure_project(self.conn, "test", Path(tmp.name))
        ticket = store.mirror_ticket(self.conn, project, "T-1", "T-1", "review",
                                     acceptance_criteria=["review"],
                                     verification_commands=["true"])
        self.run = store.claim(self.conn, project, ticket)

    def review(self, answer, prompt=None):
        def run_agent(*args, **kwargs):
            return AgentOutput(answer, "codex exec")
        if prompt is None:
            prompt = "Review.\n" + reply_brief(brief_project(self, {}), "LINES")
        return _review_reply(None, prompt, "wt", "base", "sha", self.conn,
                             self.run, run_agent=run_agent, review_round=2)

    def formats(self):
        return [json.loads(payload) for (payload,) in self.conn.execute(
            "SELECT payload FROM runEvents WHERE kind = 'review_reply_format'")]

    def test_a_markdown_reply_is_handed_on_unchanged_and_recorded_as_markdown(self):
        reply, decision, _ = self.review(MARKDOWN)
        self.assertEqual(reply, MARKDOWN)
        self.assertEqual(decision, "REQUEST_CHANGES")
        self.assertEqual([(finding["path"], finding["line"])
                          for finding in parse_findings(reply)],
                         [("src/app.py", 3)])
        self.assertEqual(self.formats(), [{"format": "markdown", "round": 2}])

    def test_a_retry_asks_each_route_for_the_reply_its_brief_describes(self):
        prompts = []

        def run_agent(target, role, prompt, *args, **kwargs):
            prompts.append(prompt)
            return AgentOutput("I could not decide.", "codex exec")
        goal = "Review.\n" + reply_brief(brief_project(self, {}), "LINES")
        _review_reply(None, goal, "wt", "base", "sha", self.conn, self.run,
                      run_agent=run_agent, review_round=2)
        retry = prompts[1]
        self.assertEqual(str(retry), retry.schema_text)
        self.assertIn("one JSON object", retry.schema_text)
        self.assertNotIn("VERDICT:", retry.schema_text)
        self.assertTrue(retry.line_text.startswith("Review.\nLINES"))
        self.assertTrue(retry.line_text.endswith(
            "VERDICT: APPROVE or VERDICT: REQUEST_CHANGES, and nothing after it."))

    def test_a_review_briefed_in_lines_of_its_own_records_no_format(self):
        reply, decision, _ = self.review(MARKDOWN, prompt="Review. End with "
                                         "VERDICT: APPROVE or REQUEST_CHANGES.")
        self.assertEqual((reply, decision), (MARKDOWN, "REQUEST_CHANGES"))
        self.assertEqual(self.formats(), [])

    def test_a_schema_valid_reply_is_rendered_and_recorded_as_json(self):
        for trailer in ("", "\ntokens used\n1,234\n"):
            with self.subTest(trailer=trailer):
                reply, decision, _ = self.review(
                    CODEX_TRANSCRIPT + json.dumps(REPLY) + trailer)
                self.assertTrue(reply.endswith("\nVERDICT: REQUEST_CHANGES"))
                self.assertEqual(decision, "REQUEST_CHANGES")
                self.assertEqual(self.formats()[-1],
                                 {"format": "json", "round": 2})

    def test_a_quoted_json_example_does_not_override_a_markdown_verdict(self):
        example = json.dumps(dict(REPLY, verdict="APPROVE", findings=[],
                                  criteria=[]))
        answer = ("- src/app.py:3 [p1] [correctness] the brief's example reads"
                  f"\n\n```json\n{example}\n```\n\nVERDICT: REQUEST_CHANGES")
        reply, decision, _ = self.review(answer)
        self.assertEqual((reply, decision), (answer, "REQUEST_CHANGES"))
        self.assertEqual(self.formats(), [{"format": "markdown", "round": 2}])

    def test_json_off_the_schema_is_left_to_the_line_parser(self):
        for broken in (dict(REPLY, verdict="LGTM"),
                       {key: REPLY[key] for key in REPLY if key != "follow_ups"},
                       dict(REPLY, findings=[dict(REPLY["findings"][0],
                                                  severity="p3")])):
            with self.subTest(broken=sorted(broken)):
                answer = f"{MARKDOWN}\n{json.dumps(broken)}"
                self.assertEqual(read_reply(answer), (answer, "markdown"))


if __name__ == "__main__":
    unittest.main()
