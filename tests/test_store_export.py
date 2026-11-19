"""`holo store export DIR` writes the store's run record as redacted NDJSON."""
import contextlib
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
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
SHA = "a" * 40


def now_ms():
    return int(time.time() * 1000)


def ms(text):
    return round(datetime.fromisoformat(text.replace("Z", "+00:00"))
                 .timestamp() * 1000)


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
        self.config = state / "config.toml"
        self.private(r"corp-internal\.example")
        self.out = root / "export"
        self.store_path = state / "store.db"
        conn = store.open(str(self.store_path))
        project_id = self.project_id = store.tickets.ensure_project(
            conn, "team-1", str(self.target))
        ticket = self.ticket = store.tickets.mirror_ticket(
            conn, project_id, linear_issue_id="issue-7", linear_identifier="KO-7",
            title="ticket")
        self.first = store.claim(conn, project_id, ticket, now=FIRST)
        store.record_event(conn, self.first, "note", LEAKY, now=FIRST + 1)
        finish_run(conn, self.first, "failed", now=FIRST + 2,
                   failure_kind="verify")
        self.second = store.claim(conn, project_id, ticket, now=SECOND)
        self.sessions_from = now_ms()
        store.record_agent_session(conn, self.second, "sess-impl", "implement",
                                   "claude")
        store.record_agent_session(conn, self.second, "sess-rev", "review",
                                   "codex")
        self.sessions_to = now_ms()
        store.record_review_round(conn, self.second, 1, "changes_requested",
                                  "codex")
        store.record_review_round(conn, self.second, 2, "pass", "codex")
        finish_run(conn, self.second, "merged", now=SECOND + 5, merge_sha=SHA)
        other = store.tickets.ensure_project(conn, "team-2", str(root / "other"))
        foreign = store.tickets.mirror_ticket(
            conn, other, linear_issue_id="issue-9", linear_identifier="ZZ-9",
            title="another project's ticket")
        self.foreign = store.claim(conn, other, foreign, now=SECOND + 10)
        store.record_event(conn, self.foreign, "note", "other project",
                           now=SECOND + 11)
        conn.close()

    def private(self, *patterns):
        listed = ", ".join(f"'{pattern}'" for pattern in patterns)
        self.config.write_text(f"[merge]\nprivate_patterns = [{listed}]\n")

    def export(self, *flags):
        with contextlib.redirect_stdout(io.StringIO()):
            status = holo_cli.main(["store", "export", str(self.out), *flags,
                                    "-p", str(self.target)])
        self.assertIn(status, (None, 0), "sys.exit(status) is not a success")

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
        self.assertEqual(
            [(r["attempt"], r["outcome"], r["failure_kind"], r["merge_sha"],
              r["review_round_count"]) for r in runs],
            [(1, "failed", "verify", None, 0), (2, "merged", None, SHA, 2)])
        self.assertEqual(
            [(r["started_at"], r["ended_at"]) for r in runs],
            [("2023-11-14T22:13:20.000Z", "2023-11-14T22:13:20.002Z"),
             ("2023-11-14T22:14:20.000Z", "2023-11-14T22:14:20.005Z")])
        sessions = lines(self.out / "agent_sessions.jsonl")
        self.assertEqual(
            [(s["session_id"], s["run_id"], s["ticket_key"], s["role"],
              s["route"]) for s in sessions],
            [("sess-impl", self.second, "KO-7", "implement", "claude"),
             ("sess-rev", self.second, "KO-7", "review", "codex")])
        for session in sessions:
            self.assertRegex(session["at"], ISO_UTC)
            self.assertTrue(
                self.sessions_from <= ms(session["at"]) <= self.sessions_to)

    def test_events_are_once_each_in_seq_order_with_matches_redacted(self):
        self.export()
        events = lines(self.out / "factory_events.jsonl")
        for event in events:
            self.assertLessEqual(
                {"run_id", "seq", "at", "kind", "summary", "payload"}, set(event))
        keys = [(e["run_id"], e["seq"]) for e in events]
        conn = sqlite3.connect(self.store_path)
        stored = conn.execute(
            "SELECT runId, seq FROM runEvents WHERE runId IN (?, ?)",
            (self.first, self.second)).fetchall()
        conn.close()
        self.assertEqual(keys, sorted(stored))
        self.assertIn("agent_session", [e["kind"] for e in events])
        (note,) = [e for e in events if e["kind"] == "note"]
        self.assertEqual(note["summary"],
                         "deploy to vault.[private] with [secret]")
        self.assertEqual(note["at"], "2023-11-14T22:13:20.001Z")
        self.assertEqual(
            [e["payload"] for e in events if e["kind"] == "agent_session"],
            [{"session_id": "sess-impl", "role": "implement", "route": "claude"},
             {"session_id": "sess-rev", "role": "review", "route": "codex"}])
        text = (self.out / "factory_events.jsonl").read_text()
        self.assertNotIn("corp-internal", text)
        self.assertNotIn("abc123def456", text)

    def test_manifest_counts_files_and_a_rerun_is_byte_identical(self):
        before_export = now_ms()
        self.export()
        after_export = now_ms()
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
        self.assertTrue(before_export <= ms(manifest["started_at"])
                        <= ms(manifest["ended_at"]) <= after_export)
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

    def test_a_store_linked_into_the_export_directory_is_refused(self):
        self.out.mkdir()
        inside = self.out / "factory_runs.jsonl"
        self.store_path.rename(inside)
        self.store_path.symlink_to(inside)
        before = inside.read_bytes()
        with self.assertRaises(SystemExit) as refused:
            self.export()
        self.assertIn("is the store", str(refused.exception))
        self.assertEqual(inside.read_bytes(), before)

    def test_an_output_symlink_is_replaced_and_its_target_left_alone(self):
        self.out.mkdir()
        elsewhere = self.out.parent / "elsewhere.txt"
        elsewhere.write_text("keep me\n")
        (self.out / "factory_runs.jsonl").symlink_to(elsewhere)
        self.export()
        self.assertEqual(elsewhere.read_text(), "keep me\n")
        self.assertFalse((self.out / "factory_runs.jsonl").is_symlink())
        self.assertEqual(len(lines(self.out / "factory_runs.jsonl")), 2)

    def test_a_rerun_keeps_an_existing_files_mode(self):
        self.out.mkdir()
        sessions = self.out / "agent_sessions.jsonl"
        sessions.write_text("")
        sessions.chmod(0o600)
        mask = os.umask(0o022)
        self.addCleanup(os.umask, mask)
        self.export()
        self.assertEqual(sessions.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            (self.out / "factory_runs.jsonl").stat().st_mode & 0o777, 0o644)

    def test_a_failed_publish_leaves_no_stale_manifest(self):
        self.export()
        (self.out / "factory_events.jsonl").unlink()
        (self.out / "factory_events.jsonl").mkdir()
        with self.assertRaises(OSError):
            self.export()
        self.assertFalse((self.out / "manifest.json").exists())
        self.assertEqual(
            sorted(p.name for p in self.out.iterdir()),
            ["agent_sessions.jsonl", "factory_events.jsonl",
             "factory_runs.jsonl"])

    def test_awkward_payloads_export_as_strict_json_losing_no_entry(self):
        conn = store.open(str(self.store_path))
        run = store.claim(conn, self.project_id, self.ticket, now=SECOND + 30)
        store.record_event(
            conn, run, "implementer_output",
            "pallbearer 12345678 done; header Bearer abc123; lab-1Bearer xyz789",
            level="detail", now=SECOND + 31, payload="{}")
        conn.execute(
            "UPDATE runEvents SET payload = ? WHERE runId = ?",
            ('{"lab-1": "up", "lab-2": "down", "size": 1e400,'
             ' "odd": "a\\ud800b", "key": "-----BEGIN PGP PRIVATE KEY'
             ' BLOCK-----\\nxyz\\n-----END PGP PRIVATE KEY BLOCK-----"}', run))
        conn.commit()
        conn.close()
        self.private("lab-[0-9]")
        self.export()

        def strict(token):
            raise ValueError(f"not strict JSON: {token}")
        text = (self.out / "factory_events.jsonl").read_text(encoding="utf-8")
        (event,) = [json.loads(line, parse_constant=strict)
                    for line in text.splitlines()
                    if json.loads(line)["run_id"] == run]
        self.assertEqual(event["summary"],
                         "pallbearer 12345678 done; header [secret];"
                         " [private][secret]")
        self.assertEqual(event["payload"], {
            "[private]": "up", "[private]#2": "down", "size": "1e400",
            "odd": "a?b", "key": "[secret]"})

    def test_a_private_match_overlapping_a_credential_still_hides_it(self):
        conn = store.open(str(self.store_path))
        run = store.claim(conn, self.project_id, self.ticket, now=SECOND + 20)
        store.record_event(
            conn, run, "note", "Authorization: Bearer zyx987wvu654;"
            " BEARER qrs321tuv098\nhost: lab-7", now=SECOND + 21)
        conn.close()
        self.private("Authorization: Bearer", "^host: lab-[0-9]+$")
        self.export()
        (note,) = [e for e in lines(self.out / "factory_events.jsonl")
                   if e["run_id"] == run]
        self.assertEqual(note["summary"], "[secret]; [secret]\n[private]")

    def test_patterns_keep_session_ids_and_redact_the_project_name(self):
        self.private("session_[a-z]+", "^repo$")
        self.export()
        sessions = lines(self.out / "agent_sessions.jsonl")
        self.assertEqual([s["session_id"] for s in sessions],
                         ["sess-impl", "sess-rev"])
        self.assertEqual({r["project"] for r in lines(
            self.out / "factory_runs.jsonl")}, {"[private]"})
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(manifest["project"], "[private]")


if __name__ == "__main__":
    unittest.main()
