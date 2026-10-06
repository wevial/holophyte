"""`holo` subcommands: one canonical row per factory.py mode, aliases over
them, each command run through the factory's own parser.

Run: python3 -m unittest discover -s tests -p 'test_holo_grammar.py' -v
"""
import collections
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli.entry
import store
import store.tickets
from holophyte.admission import state
from holophyte.cli.arguments import build_parser
from holophyte.config.project import Project
from holophyte.holo import cli as holo_cli
from holophyte.holo.grammar import ALIASES, COMMANDS, NOT_EXPOSED, factory_argv, parse

ROOT = Path(__file__).resolve().parent.parent
SHA = "a" * 40
URL = "https://example.invalid/pull/1"
T0 = 1_700_000_000_000

# words: (holo arguments, factory dest, its value, the note, other factory fields)
SAMPLES = {
    ("status",): ([], "status", True, None, {}),
    ("report",): ([], "report", True, None, {}),
    ("sweep",): (["--act"], "sweep", True, None, {"act": True}),
    ("board", "diff"): ([], "board_diff", True, None, {}),
    ("board", "import"): (["--dry-run"], "board_import", True, None,
                          {"dry_run": True}),
    ("store", "import"): (["other.db", "--dry-run"], "import_store", "other.db",
                          None, {"dry_run": True}),
    ("file",): (["T.md", "--backlog", "--priority", "high"], "file_ticket",
                "T.md", None, {"state": "Backlog", "priority": "high"}),
    ("move",): (["HOLO-1", "ready", "--revision", "2", "now"], "move",
                ["HOLO-1", "ready"], "now", {"revision": 2}),
    ("cancel",): (["HOLO-1", "--revision", "2", "a duplicate"], "cancel",
                  "HOLO-1", "a duplicate", {"revision": 2}),
    ("requeue",): (["HOLO-1", "rerun it"], "requeue", "HOLO-1", "rerun it", {}),
    ("approve",): (["HOLO-1", "ship it"], "approve", "HOLO-1", "ship it", {}),
    ("babysit",): (["HOLO-1"], "babysit", "HOLO-1", None, {}),
    ("repoint",): (["HOLO-1", SHA, "rebased"], "repoint", ["HOLO-1", SHA],
                   "rebased", {}),
    ("pause",): (["HOLO-1", "-n", "lunch"], "pause", "HOLO-1", "lunch", {}),
    ("resume",): (["HOLO-1", "back"], "resume", "HOLO-1", "back", {}),
    ("abort",): (["HOLO-1", "stop", "--close-pr"], "abort", "HOLO-1", "stop",
                 {"close_pr": True}),
    ("hold",): (["maintenance"], "hold", True, "maintenance", {}),
    ("release",): (["done"], "release_hold", True, "done", {}),
    ("close",): (["HOLO-1", URL, "landed by hand"], "close", "HOLO-1",
                 "landed by hand", {"landed": URL}),
    ("gap",): (["HOLO-1", "static", "a lint", "--found-by", "witness"],
               "gap_layer", ["HOLO-1", "static"], "a lint", {"found_by": "witness"}),
    ("story", "file"): (["slug", "--update", "HOLO-1", "--revision", "3"],
                        "file_story", "slug", None,
                        {"update": "HOLO-1", "revision": 3}),
    ("story", "approve"): (["HOLO-1", "--revision", "3", "red as planned",
                            "--baseline-red-kind", "exception", "w1"],
                           "approve_story", "HOLO-1", "red as planned",
                           {"baseline_red_kind": [["exception", "w1"]]}),
    ("story", "witness"): (["HOLO-1"], "witness_pass", "HOLO-1", None, {}),
    ("story", "decide"): (["HOLO-1", "1", "2", "take two"], "decide",
                          ["HOLO-1", "1", "2"], "take two", {}),
    ("supervise",): (["--once"], "supervise", True, None, {"once": True}),
    ("serve",): (["7710"], "serve", "7710", None, {}),
}


def factory_args(holo_argv, project="/repo"):
    argv = factory_argv(parse(holo_cli.build_parser(), holo_argv))
    return build_parser()[0].parse_args([project, *argv])


class TableTests(unittest.TestCase):
    def test_every_parser_mode_has_one_canonical_row_and_only_worker_is_hidden(self):
        _, modes = build_parser()
        registered = {action.option_strings[0] for action in modes._group_actions}
        rows = collections.Counter(command.mode for command in COMMANDS)
        self.assertEqual({mode for mode, count in rows.items() if count > 1}, set())
        self.assertEqual(registered - set(rows), set(NOT_EXPOSED))
        self.assertEqual(set(rows) - registered, set())
        self.assertEqual(set(NOT_EXPOSED), {"--worker"})

    def test_each_row_translates_to_its_mode_and_note_in_the_factory_parser(self):
        self.assertEqual(set(SAMPLES), {command.words for command in COMMANDS})
        for words, (rest, dest, value, note, fields) in SAMPLES.items():
            with self.subTest(words=words):
                args = factory_args([*words, *rest])
                self.assertEqual(args.target, "/repo")
                self.assertEqual(getattr(args, dest), value)
                self.assertEqual(args.note, note)
                for field, expected in fields.items():
                    self.assertEqual(getattr(args, field), expected)

    def test_the_last_positional_is_the_note_even_when_it_looks_like_an_option(self):
        decide = factory_args(["story", "decide", "HOLO-1", "1", "why"])
        self.assertEqual((decide.decide, decide.note), (["HOLO-1", "1"], "why"))
        for note in ("2", "default"):
            numeric = factory_args(["story", "decide", "HOLO-1", "1", note])
            self.assertEqual((numeric.decide, numeric.note), (["HOLO-1", "1"], note))

    def test_a_dash_leading_note_after_a_flag_is_the_note_and_a_flag_is_not(self):
        for note in ("-1 was wrong", "-1"):
            cancel = factory_args(["cancel", "HOLO-1", "--revision", "2", note,
                                   "-p", "/repo"])
            self.assertEqual((cancel.cancel, cancel.revision, cancel.note),
                             ("HOLO-1", 2, note))
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as refused:
            factory_args(["cancel", "HOLO-1", "--revision", "2", "--bogus"])
        self.assertEqual(refused.exception.code, 2)

    def test_every_alias_names_a_canonical_command_and_shadows_none(self):
        canonical = [command.words for command in COMMANDS]
        aliases = [alias for alias, _ in ALIASES]
        self.assertEqual(len(set(canonical)), len(canonical))
        self.assertEqual(len(set(aliases)), len(aliases))
        self.assertEqual(set(aliases) & set(canonical), set())
        self.assertEqual([words for _, words in ALIASES if words not in canonical], [])
        self.assertIn((("ticket", "requeue"), ("requeue",)), ALIASES)


def holo(*args, home):
    return subprocess.run(
        [sys.executable, "-m", "holophyte.holo", *args], cwd=ROOT,
        capture_output=True, text=True,
        env={**os.environ, "HOLOPHYTE_HOME": str(home), "PYTHONPATH": str(ROOT)})


def factory(*args, home):
    return subprocess.run(
        [sys.executable, str(ROOT / "factory.py"), *args], cwd=ROOT,
        capture_output=True, text=True,
        env={**os.environ, "HOLOPHYTE_HOME": str(home), "PYTHONPATH": str(ROOT)})


class Home(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        patcher = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def repo(self, name, prefix=None):
        path = self.root / name
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        target = Project.locate(path, adopt=False)
        target.holo_dir.mkdir(parents=True, exist_ok=True)
        target.config_path.write_text(
            f'[board]\nkind = "native"\nprefix = "{prefix or name.upper()}"\n'
            f'[serve]\nname = "{name}"\n')
        return path


class RequeueTests(Home):
    def setUp(self):
        super().setUp()
        self.path = self.repo("repo", "HOLO")
        self.conn = store.open(str(Project.locate(self.path).store_path))
        self.addCleanup(self.conn.close)
        project = store.tickets.ensure_project(self.conn, "native:HOLO", self.path)
        self.ticket = store.tickets.mirror_ticket(
            self.conn, project, linear_issue_id="issue-1",
            linear_identifier="HOLO-1", title="a ticket",
            acceptance_criteria=["Given a ticket, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * 60_000)
        store.tickets.transition(self.conn, self.ticket, "in_flight")
        self.run = store.claim(self.conn, project, self.ticket, now=T0)

    def fail_the_run(self):
        store.release(self.conn, self.run, "failed", "verify failed", now=T0 + 1)

    def status(self):
        return self.conn.execute("SELECT status FROM tickets WHERE id = ?",
                                 (self.ticket,)).fetchone()[0]

    def interventions(self):
        return self.conn.execute(
            'SELECT runId, "action" FROM interventions WHERE "action" != \'migrate\''
            " ORDER BY id").fetchall()

    def assert_requeued(self, *words):
        self.fail_the_run()
        result = holo(*words, "HOLO-1", "rerun it", "-p", str(self.path),
                      home=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.status(), "ready")
        self.assertEqual(self.interventions(), [(self.run, "requeue")])
        (summary,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind = 'intervention'",
            (self.run,)).fetchone()
        self.assertIn("requeue: rerun it", summary)

    def test_requeue_verb_first_requeues_the_failed_ticket(self):
        self.assert_requeued("requeue")

    def test_ticket_requeue_noun_first_requeues_it_the_same_way(self):
        self.assert_requeued("ticket", "requeue")

    def test_requeue_without_a_note_exits_two_with_the_factory_refusal(self):
        self.fail_the_run()
        before = list(self.conn.iterdump())
        result = holo("requeue", "HOLO-1", "-p", str(self.path), home=self.home)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--requeue records why the ticket goes back in the queue",
                      result.stderr)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_requeue_of_a_live_run_is_refused_as_factory_requeue_refuses_it(self):
        before = list(self.conn.iterdump())
        result = holo("requeue", "HOLO-1", "rerun it", "-p", str(self.path),
                      home=self.home)
        legacy = factory("--requeue", "HOLO-1", "--note", "rerun it", str(self.path),
                         home=self.home)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((result.returncode, result.stderr),
                         (legacy.returncode, legacy.stderr))
        self.assertTrue(result.stderr.strip())
        self.assertEqual(list(self.conn.iterdump()), before)


class HostTests(Home):
    def register(self, name):
        path = self.repo(name)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(holophyte.cli.entry.cli(["project", "add", str(path)]))
        return path

    def admission(self, path):
        target = Project.locate(path, adopt=False)
        conn = store.open(str(target.store_path))
        try:
            return state(conn, target)
        finally:
            conn.close()

    def test_hold_finds_a_project_by_its_serve_name_or_its_path(self):
        alpha, beta = self.register("alpha"), self.register("beta")
        by_name = holo("hold", "maintenance", "-p", "alpha", home=self.home)
        self.assertEqual(by_name.returncode, 0, by_name.stderr)
        self.assertEqual(self.admission(alpha), ("held", "maintenance"))
        self.assertEqual(self.admission(beta), ("enabled", None))
        by_path = holo("hold", "-n", "disk swap", "-p", str(beta), home=self.home)
        self.assertEqual(by_path.returncode, 0, by_path.stderr)
        self.assertEqual(self.admission(beta), ("held", "disk swap"))

    def test_status_json_with_no_project_is_the_host_form(self):
        self.register("alpha")
        result = holo("status", "--json", home=self.home)
        legacy = factory("--status", "--json", home=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual([project["name"] for project in document["projects"]],
                         ["alpha"])
        self.assertEqual(document, json.loads(legacy.stdout))

    def test_project_list_prints_what_factory_project_list_prints(self):
        self.register("alpha")
        self.register("beta")
        result = holo("project", "list", home=self.home)
        legacy = factory("project", "list", home=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("alpha", result.stdout)
        self.assertEqual(result.stdout, legacy.stdout)


if __name__ == "__main__":
    unittest.main()
