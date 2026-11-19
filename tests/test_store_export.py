"""`holo store export DIR` writes the store's run record as redacted NDJSON."""
import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.config.project
import store
import store.tickets
from holophyte.holo import cli as holo_cli
from tests.phase_fixture import finish_run

FIRST = 1_700_000_000_000  # 2023-11-14T22:13:20Z
SECOND = FIRST + 60_000
ISO_UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
LEAKY = "deploy to vault.corp-internal.example with Bearer abc123def456"


def lines(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


class StoreExportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.target = root / "repo"
        self.target.mkdir()
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(root / "home"),
                                       "HOME": str(root)})
        home.start()
        self.addCleanup(home.stop)
        state = holophyte.config.project.state_dir(self.target)
        state.mkdir(parents=True)
        (state / "config.toml").write_text(
            "[merge]\nprivate_patterns = ['corp-internal\\.example']\n")
        self.out = root / "export"
        conn = store.open(str(state / "store.db"))
        project = store.tickets.ensure_project(conn, "team-1", str(self.target))
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-7", linear_identifier="KO-7",
            title="ticket")
        self.first = store.claim(conn, project, ticket, now=FIRST)
        store.record_event(conn, self.first, "note", LEAKY, now=FIRST + 1)
        finish_run(conn, self.first, "failed", now=FIRST + 2)
        self.second = store.claim(conn, project, ticket, now=SECOND)
        store.record_agent_session(conn, self.second, "sess-impl", "implement",
                                   "claude")
        store.record_agent_session(conn, self.second, "sess-rev", "review",
                                   "codex")
        finish_run(conn, self.second, "merged", now=SECOND + 5)
        conn.close()

    def export(self, *flags):
        with contextlib.redirect_stdout(io.StringIO()):
            holo_cli.main(["store", "export", str(self.out), *flags,
                           "-p", str(self.target)])

    def test_runs_and_agent_sessions_carry_their_fields(self):
        self.export()
        runs = lines(self.out / "factory_runs.jsonl")
        self.assertEqual([r["run_id"] for r in runs], [self.first, self.second])
        for run in runs:
            self.assertLessEqual(
                {"run_id", "ticket_key", "project", "attempt", "outcome",
                 "failure_kind", "merge_sha", "review_round_count",
                 "started_at", "ended_at"}, set(run))
            self.assertEqual(run["ticket_key"], "KO-7")
            self.assertEqual(run["project"], "repo")
            self.assertRegex(run["started_at"], ISO_UTC)
            self.assertRegex(run["ended_at"], ISO_UTC)
        self.assertEqual([r["attempt"] for r in runs], [1, 2])
        self.assertEqual([r["outcome"] for r in runs], ["failed", "merged"])
        self.assertEqual(runs[0]["started_at"], "2023-11-14T22:13:20.000Z")
        sessions = lines(self.out / "agent_sessions.jsonl")
        self.assertEqual(
            [(s["session_id"], s["run_id"], s["ticket_key"], s["role"],
              s["route"]) for s in sessions],
            [("sess-impl", self.second, "KO-7", "implement", "claude"),
             ("sess-rev", self.second, "KO-7", "review", "codex")])
        for session in sessions:
            self.assertRegex(session["at"], ISO_UTC)

    def test_events_are_once_each_in_seq_order_with_matches_redacted(self):
        self.export()
        events = lines(self.out / "factory_events.jsonl")
        for event in events:
            self.assertLessEqual(
                {"run_id", "seq", "at", "kind", "summary", "payload"}, set(event))
        keys = [(e["run_id"], e["seq"]) for e in events]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys, sorted(keys))
        (note,) = [e for e in events if e["kind"] == "note"]
        self.assertEqual(note["summary"],
                         "deploy to vault.[private] with [secret]")
        text = (self.out / "factory_events.jsonl").read_text()
        self.assertNotIn("corp-internal", text)
        self.assertNotIn("abc123def456", text)

    def test_manifest_counts_files_and_a_rerun_is_byte_identical(self):
        self.export()
        names = ("factory_runs.jsonl", "factory_events.jsonl",
                 "agent_sessions.jsonl")
        before = {name: (self.out / name).read_bytes() for name in names}
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(manifest["project"], "repo")
        self.assertEqual(manifest["files"], {
            name: len(before[name].splitlines()) for name in names})
        self.assertEqual(manifest["files"]["agent_sessions.jsonl"], 2)
        self.assertRegex(manifest["started_at"], ISO_UTC)
        self.assertRegex(manifest["ended_at"], ISO_UTC)
        self.assertIsNone(manifest["since"])
        self.assertIsInstance(manifest["redaction_version"], int)
        self.export()
        self.assertEqual(
            {name: (self.out / name).read_bytes() for name in names}, before)

    def test_since_keeps_only_runs_started_at_or_after_it(self):
        self.export("--since", str(SECOND))
        self.assertEqual([r["run_id"] for r in lines(
            self.out / "factory_runs.jsonl")], [self.second])
        self.assertEqual({e["run_id"] for e in lines(
            self.out / "factory_events.jsonl")}, {self.second})
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(manifest["since"], SECOND)


if __name__ == "__main__":
    unittest.main()
