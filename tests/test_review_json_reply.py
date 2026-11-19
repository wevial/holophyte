"""Reviewer replies in the output schema's JSON: the Codex reviewer argv names
the schema, a schema-valid reply renders to the line format every reader
parses, and any other reply is parsed as it always was."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store  # noqa: E402
from holophyte.agents.agent_output import AgentOutput  # noqa: E402
from holophyte.agents.harness import ADAPTERS, Seat  # noqa: E402
from holophyte.babysit.description_review import (  # noqa: E402
    description_findings,
    without_description,
)
from holophyte.review.json_reply import read_reply  # noqa: E402
from holophyte.review.reply_parsing import (  # noqa: E402
    FINDING_CATEGORIES,
    _review_reply,
    criteria_findings,
    criterion_accounts,
    follow_up_lines,
    parse_findings,
)
from holophyte.review.review_runner import terminal_verdict  # noqa: E402

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
                       message="`handlers[0](event)` raises on an empty list")
        text, _ = read_reply(json.dumps(dict(REPLY, findings=[finding])))
        [parsed] = parse_findings(text)
        self.assertIn("`handlers[0](event)` raises", parsed["message"])

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

    def test_a_follow_up_does_not_hide_a_description_only_blocker(self):
        reply = dict(REPLY, findings=[REPLY["findings"][1]])
        text, _ = read_reply(json.dumps(reply))
        self.assertEqual(description_findings(text, "REQUEST_CHANGES", []),
                         ['- path: "PR description" [p2] The summary promises'
                          ' a retry the diff does not add.'])

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

    def review(self, answer):
        def run_agent(*args, **kwargs):
            return AgentOutput(answer, "codex exec")
        return _review_reply(None, "review", "wt", "base", "sha", self.conn,
                             self.run, run_agent=run_agent, review_round=2)

    def formats(self):
        return [json.loads(payload) for (payload,) in self.conn.execute(
            "SELECT payload FROM runEvents WHERE kind = 'review_reply_format'")]

    def test_a_markdown_reply_is_handed_on_unchanged_and_recorded_as_markdown(self):
        reply, decision, _ = self.review(MARKDOWN)
        self.assertEqual(reply, MARKDOWN)
        self.assertEqual(decision, "REQUEST_CHANGES")
        self.assertEqual(self.formats(), [{"format": "markdown", "round": 2}])

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
