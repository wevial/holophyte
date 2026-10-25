"""A scout titled by the `[questions.titles]` route, through fake `claude` and
`codex` executables on PATH and a real project store."""

import json
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.tickets
from holophyte import question_cli, questions, redact
from holophyte.config.checks import check_config_keys
from holophyte.config.project import Project
from holophyte.scout import titler
from store.scouts import create_scout, move_scout, scout
from tests.test_question_backends import (
    FAKE,
    FIXTURES,
    claude_output,
    codex_output,
)

DOCS = Path(__file__).resolve().parents[1] / "docs" / "config.md"
ASK = ("How long does test_babysit_threads.py take against its 90 s ceiling?"
       " Use SECRET-TOKEN-1 if needed.")
TITLE_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}},
                "required": ["title"], "additionalProperties": False}


def codex_title(document):
    lines = (FIXTURES / "codex_answer.jsonl").read_text().splitlines()
    for index, line in enumerate(lines):
        event = json.loads(line)
        if event.get("item", {}).get("type") == "agent_message":
            event["item"]["text"] = json.dumps(document)
            lines[index] = json.dumps(event)
    return "\n".join(lines) + "\n"


def claude_title(document):
    answer = json.loads((FIXTURES / "claude_answer.json").read_text())
    answer["structured_output"] = document
    answer["result"] = json.dumps(document)
    return json.dumps(answer)


def titles(table):
    return {"questions": {"titles": table}}


class TitleSettingsTests(unittest.TestCase):
    def test_the_titles_seat_defaults_to_a_cheap_codex_route(self):
        self.assertIsNone(questions.settings({})["titles"])
        empty = questions.settings(titles({}))["titles"]
        self.assertEqual(
            [empty[key] for key in ("backend", "model", "effort",
                                    *questions.FALLBACK_KEYS)],
            ["codex", "gpt-6-luna", "low", None, None, None])
        claude = questions.settings(titles({"backend": "claude"}))["titles"]
        self.assertEqual((claude["model"], claude["effort"]), ("haiku", "low"))

    def test_jev_and_unknown_keys_are_refused_naming_the_seat(self):
        for table, pattern in (
                ({"backend": "jev"}, r"backend must be claude or codex"),
                ({"backend_fallback": "jev"},
                 r"backend_fallback must be claude or codex"),
                ({"colour": "red"},
                 r"colour: unknown key; \[questions\.titles\] accepts: "
                 r"backend, backend_fallback, effort, fallback_effort, "
                 r"fallback_model, model")):
            with self.subTest(table=table), self.assertRaisesRegex(
                    ValueError, r"\[questions\.titles\] " + pattern):
                questions.settings(titles(table))

    def test_the_reference_example_passes_the_project_config_reader(self):
        section = DOCS.read_text().split("## `[questions.titles]`", 1)[1]
        example = re.search(r"```toml\n(.*?)```", section, re.DOTALL).group(1)
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(["git", "init", "-q", root], check=True)
            project = Project.locate(Path(root), adopt=False)
            project.holo_dir.mkdir(parents=True)
            project.config_path.write_text(example)
            check_config_keys(project)
            options = questions.settings(project.config())["titles"]
        self.assertEqual(
            [options[key] for key in ("backend", "model", "effort",
                                      *questions.FALLBACK_KEYS)],
            ["codex", "gpt-6-luna", "low", "claude", "haiku", "low"])


class ScoutTitleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = Path(tmp.name) / "bin"
        self.bin.mkdir()
        for name in ("claude", "codex"):
            path = self.bin / name
            path.write_text(FAKE)
            path.chmod(0o755)
        self.fake("claude", "probe", claude_output("ready", 0.99))
        self.fake("codex", "probe", codex_output("ready", 0.99))
        self.enterContext(patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"}))
        question_cli.ROUTES.clear()
        self.addCleanup(question_cli.ROUTES.clear)
        self.conn = store.open(Path(tmp.name) / "store.db")
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(self.conn, "team",
                                                       "/repos/example")

    def fake(self, name, mode, output, exit_code=0):
        (self.bin / f"{name}.{mode}.out").write_text(output)
        (self.bin / f"{name}.{mode}.exit").write_text(str(exit_code))

    def calls(self, name):
        log = self.bin / f"{name}.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def a_scout(self, ask=ASK, kind="bug"):
        scout_id = create_scout(self.conn, self.project_id, ask, kind, 20)
        move_scout(self.conn, scout_id, "planning", base_sha="9" * 40,
                   host="writer")
        return scout(self.conn, scout_id)

    def titled(self, row, config):
        titler.title_scout(self.conn, config, row)
        (stored,) = self.conn.execute("SELECT title FROM scouts WHERE id = ?",
                                      (row.id,)).fetchone()
        events = [json.loads(payload) for (payload,) in self.conn.execute(
            "SELECT payload FROM runEvents WHERE scoutId = ?"
            " AND kind = 'scout_title'", (row.id,))]
        return stored, events

    def test_codex_names_the_scout_from_its_redacted_ask(self):
        self.fake("codex", "question", codex_title(
            {"title": "“Measure the babysit thread test.”"}))
        row = self.a_scout()
        with redact.values_held(["SECRET-TOKEN-1"]):
            stored, events = self.titled(row, titles({}))
        (call,) = [c for c in self.calls("codex") if c["mode"] == "question"]
        argv = call["argv"]
        for flags in (["-s", "read-only"], ["--disable", "shell_tool"],
                      ["--ephemeral"], ["-m", "gpt-6-luna"],
                      ["-c", "model_reasoning_effort=low"]):
            self.assertIn(flags, [argv[i:i + len(flags)]
                                  for i in range(len(argv))])
        self.assertEqual(json.loads(call["schema"]), TITLE_SCHEMA)
        text = argv[-1]
        self.assertIn(titler.INSTRUCTION, text)
        record = json.loads(re.search(r"```json\n(.*?)\n```", text,
                                      re.DOTALL).group(1))
        self.assertEqual(record, {
            "ask": ASK.replace("SECRET-TOKEN-1", redact.REDACTED),
            "kind": "bug"})
        for logged in self.calls("codex"):
            self.assertNotIn("SECRET-TOKEN-1", json.dumps(logged["argv"]))
            self.assertNotIn("SECRET-TOKEN-1", logged["schema"])
        self.assertEqual(self.calls("claude"), [])
        self.assertEqual(stored, "Measure the babysit thread test")
        (event,) = events
        self.assertEqual(
            (event["title"], event["backend"], event["model"], event["why"]),
            ("Measure the babysit thread test", "codex", "gpt-6-luna", None))

    def test_a_failed_codex_probe_falls_back_to_claude_and_is_recorded(self):
        self.fake("codex", "probe", codex_output("ready", 0.99), exit_code=1)
        self.fake("claude", "question",
                  claude_title({"title": "Babysit thread timing"}))
        row = self.a_scout()
        stored, events = self.titled(
            row, titles({"backend_fallback": "claude"}))
        (call,) = [c for c in self.calls("claude") if c["mode"] == "question"]
        argv = call["argv"]
        for flag, value in (("--tools", ""), ("--model", "haiku"),
                            ("--effort", "low")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]),
                         TITLE_SCHEMA)
        self.assertEqual(stored, "Babysit thread timing")
        self.assertEqual([event["backend"] for event in events], ["claude"])
        (intervention,) = self.conn.execute(
            "SELECT runId, guidance FROM interventions"
            " WHERE action = 'route_fallback'").fetchall()
        self.assertIsNone(intervention[0])
        self.assertEqual(json.loads(intervention[1])["seat"],
                         "questions.titles")

    def test_a_refused_or_failed_answer_stores_no_title(self):
        long_title = "Measure how long every babysit thread test takes on slow host"
        self.assertEqual(len(long_title), 61)
        for output, exit_code, why in (
                (codex_title({"title": long_title}), 0, "invalid_response"),
                (codex_title({"title": "  "}), 0, "invalid_response"),
                (codex_title({"title": "Babysit thread timing"}), 1,
                 "service_error")):
            with self.subTest(why=why, exit_code=exit_code):
                self.fake("codex", "question", output, exit_code)
                stored, events = self.titled(self.a_scout(), titles({}))
                self.assertIsNone(stored)
                self.assertEqual([(e["title"], e["why"]) for e in events],
                                 [(None, why)])

    def test_with_every_route_down_no_title_question_is_launched(self):
        self.fake("codex", "probe", codex_output("ready", 0.99), exit_code=1)
        self.fake("claude", "probe", claude_output("ready", 0.99), exit_code=1)
        stored, events = self.titled(
            self.a_scout(), titles({"backend_fallback": "claude"}))
        self.assertIsNone(stored)
        self.assertEqual([(e["title"], e["why"]) for e in events],
                         [(None, "route_down")])
        modes = [c["mode"] for name in ("codex", "claude")
                 for c in self.calls(name)]
        self.assertEqual(modes, ["probe", "probe"])


class ScoutTitleColumnTests(unittest.TestCase):
    def test_a_store_at_the_previous_version_gains_a_null_scout_title(self):
        self.assertEqual(store.schema.READABLE_FROM, 50)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = store.open(path)
            project = store.tickets.ensure_project(conn, "team", "/repos/h")
            scout_id = create_scout(conn, project, "Why does it spin?", "bug",
                                    20)
            conn.close()
            raw = sqlite3.connect(path)
            raw.executescript("ALTER TABLE scouts DROP COLUMN title;\n"
                              "PRAGMA user_version = 61;\n")
            raw.close()

            conn = store.open(path)
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                             (62,))
            self.assertEqual(scout(conn, scout_id).title, None)
            self.assertEqual(scout(conn, scout_id).ask, "Why does it spin?")


if __name__ == "__main__":
    unittest.main()
