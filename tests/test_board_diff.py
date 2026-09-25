"""`factory.py PROJECT --board-diff`: the store's copy of the ready queue
held against a `FileProvider` board's listing, read-only on both (KO-738).

Run: python3 -m unittest discover -s tests -p 'test_board_diff.py' -v
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.board
import holophyte.cli
import holophyte.project
import store.tickets
from holophyte.board_diff import board_diff
from holophyte.runs import open_store
from provider import FileProvider

TICKET = """\
# {title}

## Acceptance criteria

- [ ] Given the ticket, when it is worked, then it is done.

## Verify command(s)

```
echo ok
```
"""


class BoardDiffTests(unittest.TestCase):
    """A target whose store mirrored a two-ticket file board."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = patch.dict(os.environ,
                             {"HOLOPHYTE_HOME": str(self.root / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.boards = self.root / "KO"
        self.boards.mkdir()
        for identifier in ("KO-1", "KO-2"):
            (self.boards / f"{identifier}.md").write_text(
                TICKET.format(title=f"ticket {identifier}"))
        self.board = FileProvider(self.boards)
        self.target = holophyte.project.Project.locate(self.repo)
        conn = open_store(self.target)
        try:
            project = store.tickets.ensure_project(conn, self.board.team,
                                                   self.repo)
            for task in self.board.ready_issues():
                holophyte.board.mirror_task(conn, project, task)
        finally:
            conn.close()

    def snapshot(self):
        """The store file's bytes and every board file's, by name."""
        return (self.target.store_path.read_bytes(),
                {p.name: p.read_bytes() for p in self.boards.iterdir()})

    def diff(self):
        out = io.StringIO()
        before = self.snapshot()
        status = board_diff(self.target, self.board, out=out)
        self.assertEqual(self.snapshot(), before)
        return status, out.getvalue().splitlines()

    def test_a_board_unchanged_since_the_mirror_has_no_differences(self):
        status, lines = self.diff()
        self.assertEqual(lines, ["[holo2] board diff: no differences"])
        self.assertEqual(status, 0)

    def test_each_kind_of_difference_is_one_line_then_a_count(self):
        (self.boards / "KO-1.title").write_text("a retitled ticket\n")
        (self.boards / "KO-3.md").write_text(TICKET.format(title="new"))
        (self.boards / "KO-2.state").write_text("Backlog\n")
        status, lines = self.diff()
        self.assertEqual(len(lines), 4, lines)
        self.assertRegex(lines[0], r"^KO-1: title differs.*a retitled ticket")
        self.assertRegex(lines[1], r"^KO-3: .*not in the store")
        self.assertRegex(lines[2], r"^KO-2: ready in the store, not on the")
        self.assertEqual(lines[3], "[holo2] board diff: 3 differences")
        self.assertEqual(status, 1)

    def test_the_flag_returns_the_diff_status_without_a_loop(self):
        (self.boards / "KO-2.state").write_text("Backlog\n")
        self.target.holo_dir.mkdir(parents=True, exist_ok=True)
        self.target.config_path.write_text('[board]\nteam = "KO"\n'
                                           'project_id = "project-1"\n')
        out = io.StringIO()
        with patch.object(holophyte.cli, "board_for",
                          lambda target: self.board), \
                patch.object(holophyte.cli, "main") as loop, \
                patch.object(holophyte.cli, "supervise") as supervisor, \
                patch.object(holophyte.cli, "start_supervisor") as spawn, \
                contextlib.redirect_stdout(out):
            status = holophyte.cli.cli([str(self.repo), "--board-diff"])
        self.assertEqual(status, 1)
        self.assertIn("[holo2] board diff: 1 difference", out.getvalue())
        loop.assert_not_called()
        supervisor.assert_not_called()
        spawn.assert_not_called()
