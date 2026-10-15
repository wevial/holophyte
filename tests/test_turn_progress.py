"""A streaming Claude turn records a narrative `turn_progress` event: its
tool-call count and the latest call's label, at most once a period.

Run: python3 -m unittest discover -s tests -p 'test_turn_progress.py' -v
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_harness  # noqa: E402

import holophyte.agents.turn_progress  # noqa: E402
import holophyte.loop.implement  # noqa: E402
import store  # noqa: E402
import store.read  # noqa: E402

INIT = {"type": "system", "subtype": "init", "session_id": "S",
        "cwd": "/workspace", "tools": ["Bash", "Edit", "Read"]}
RESULT = {"type": "result", "subtype": "success", "is_error": False,
          "num_turns": 3, "result": "finished work", "session_id": "S"}
SECRET = "lin_api_Zq81rTsecretvalue"


def tool_use(name, parent=None, **given):
    return {"type": "assistant", "session_id": "S", "parent_tool_use_id": parent,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": name,
                 "input": given}]}}


class StreamedTurnCase(unittest.TestCase):
    setUp = test_harness.ClaudeTableTests.setUp

    def turn_printing(self, body, config="", every=1):
        binary = self.repo.parent / "claude-harness"
        binary.write_text(f"#!{sys.executable}\nimport json, sys, time\n"
                          "def say(line):\n    print(json.dumps(line), flush=True)\n"
                          f"{body}")
        binary.chmod(0o755)
        self.target.config_path.write_text(
            test_harness.CONFIG + config + f'[harnesses]\nclaude = "{binary}"\n')
        with patch.object(holophyte.agents.turn_progress, "PROGRESS_EVERY_S",
                          every):
            output, timed_out = holophyte.loop.implement._timed(
                self.target, self.conn, self.run, 60, self.repo, 1,
                "implement the thing")
        self.assertFalse(timed_out)
        return output

    def progress(self):
        return [event.summary for event in
                store.read.narrative_events_after(self.conn, 0, 0)
                if event.runId == self.run and event.kind == "turn_progress"]


class TurnProgressTests(StreamedTurnCase):
    def test_the_first_call_records_and_a_later_one_waits_out_the_period(self):
        output = self.turn_printing(
            f"say({INIT!r})\n"
            f"say({tool_use('Read', file_path=f'{self.repo}/README.md')!r})\n"
            f"say({tool_use('Edit', file_path='/workspace/app.txt')!r})\n"
            "time.sleep(1.5)\n"
            f"say({tool_use('Bash', command='make', description='Run unit tests')!r})\n"
            f"say({RESULT!r})\n")
        self.assertEqual(output, "finished work")
        self.assertEqual(self.progress(), [
            "implement turn: 1 tool call; last: reading README.md",
            "implement turn: 3 tool calls; last: running Run unit tests"])

    def test_a_burst_of_calls_records_at_most_once_a_period(self):
        edit = tool_use("Edit", file_path="/workspace/app.txt")
        self.turn_printing(
            f"say({INIT!r})\n"
            "for burst in range(5):\n"
            f"    for _ in range(100):\n        say({edit!r})\n"
            "    time.sleep(0.45)\n"
            f"say({RESULT!r})\n")
        progress = self.progress()
        self.assertLessEqual(len(progress), 3, progress)
        last = int(progress[-1].split(": ")[1].split(" ")[0])
        self.assertGreaterEqual(last, 250, progress)

    def test_a_credential_in_a_description_is_redacted(self):
        self.turn_printing(
            f"say({tool_use('Bash', description=f'curl with {SECRET}')!r})\n"
            f"say({RESULT!r})\n",
            config=f'[linear]\napi_key = "{SECRET}"\n')
        [summary] = self.progress()
        self.assertTrue(summary.startswith(
            "implement turn: 1 tool call; last: running curl with "))
        self.assertNotIn(SECRET, summary)

    def test_a_failed_progress_write_leaves_the_turn_and_its_record(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            output = self.turn_printing(
                "import sqlite3\n"
                f"db = sqlite3.connect({str(self.target.store_path)!r})\n"
                "columns = [r[1] for r in db.execute('PRAGMA table_info(runs)')]\n"
                f"key = ({self.run},)\n"
                "row = db.execute('SELECT * FROM runs WHERE id = ?', key).fetchone()\n"
                "db.execute('DELETE FROM runs WHERE id = ?', key)\n"
                "db.commit()\n"
                f"say({tool_use('Bash', description='Run unit tests')!r})\n"
                "time.sleep(1)\n"
                "db.execute(f\"INSERT INTO runs ({', '.join(columns)}) VALUES"
                " ({', '.join('?' * len(columns))})\", row)\n"
                "db.commit()\n"
                f"say({RESULT!r})\n")
        self.assertEqual(output, "finished work")
        self.assertEqual(self.progress(), [])
        self.assertIn(f"no run {self.run}", printed.getvalue())
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM runEvents WHERE runId = ? AND kind = 'agent_turn'",
            (self.run,)).fetchone(), (1,))

    def test_an_opaque_command_string_records_no_progress(self):
        script = self.repo.parent / "opaque-implementer"
        script.write_text(
            f"#!{sys.executable}\nimport json\n"
            f"print(json.dumps({tool_use('Edit', file_path='/workspace/app.txt')!r}))\n"
            f"print(json.dumps({RESULT!r}))\n")
        script.chmod(0o755)
        self.target.config_path.write_text(f'[agents]\nimplementer = "{script}"\n')
        with patch.object(holophyte.agents.turn_progress, "PROGRESS_EVERY_S", 0):
            holophyte.loop.implement._timed(self.target, self.conn, self.run, 60,
                                            self.repo, 1, "implement the thing")
        self.assertEqual(self.progress(), [])


class LabelTests(StreamedTurnCase):
    def label_of(self, line):
        recorder = holophyte.agents.turn_progress.TurnProgress(
            self.conn, self.run, "implement", self.repo, frozenset())
        with patch.object(holophyte.agents.turn_progress, "PROGRESS_EVERY_S", 0):
            recorder.seen(line)
        summary = self.progress()[-1]
        self.assertTrue(summary.startswith("implement turn: 1 tool call; last: "))
        return summary.removeprefix("implement turn: 1 tool call; last: ")

    def test_real_shaped_tool_calls_read_as_short_labels(self):
        for line, label in (
                (tool_use("Edit", file_path="/workspace/holophyte/x.py",
                          old_string="a", new_string="b"),
                 "editing holophyte/x.py"),
                (tool_use("Agent", description="Review the diff",
                          prompt="Read the diff", subagent_type="general-purpose"),
                 "subagent: Review the diff"),
                (tool_use("Bash", parent="toolu_01Parent", command="echo ok",
                          description="Print ok"),
                 "subagent running Print ok"),
                (tool_use("Bash", command="echo hunter2-marker"),
                 "running a command"),
                (tool_use("WebFetch", url="https://example.invalid", prompt="p"),
                 "WebFetch")):
            with self.subTest(label):
                self.assertEqual(self.label_of(line), label)

    def test_a_credential_cut_by_the_description_limit_is_still_redacted(self):
        recorder = holophyte.agents.turn_progress.TurnProgress(
            self.conn, self.run, "implement", self.repo, frozenset({SECRET}))
        with patch.object(holophyte.agents.turn_progress, "PROGRESS_EVERY_S", 0):
            recorder.seen(tool_use("Bash", description="x" * 56 + SECRET))
        [summary] = self.progress()
        self.assertEqual(summary, "implement turn: 1 tool call; last: running "
                                  + "x" * 56 + "[redacted]")

    def test_a_line_without_a_tool_call_records_nothing_more(self):
        recorder = holophyte.agents.turn_progress.TurnProgress(
            self.conn, self.run, "implement", self.repo, frozenset())
        text = {"type": "assistant", "session_id": "S", "parent_tool_use_id": None,
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Running the tests now."}]}}
        with patch.object(holophyte.agents.turn_progress, "PROGRESS_EVERY_S", 0):
            recorder.seen(tool_use("Grep", pattern="def seen"))
            recorder.seen(text)
        self.assertEqual(self.progress(),
                         ["implement turn: 1 tool call; last: searching"])


if __name__ == "__main__":
    unittest.main()
