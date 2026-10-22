"""A console note's text never leaves for GitHub in a pushed commit message.

Run: python3 -m unittest discover -s tests -p 'test_note_privacy.py' -v
"""
from __future__ import annotations

import store
from store.operator_notes import send_back
from tests.fake_agent import APPROVE, Commit, Idle
from tests.loop_fixture import MergeModeFixture

NOTE = ("drop the legacy export flag and keep the importer reading"
        " the old column names")
WITHHELD = "[maintainer note withheld]"


class SendBackPushTests(MergeModeFixture):
    def test_a_fix_commit_quoting_a_send_back_is_pushed_with_the_note_withheld(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(states=[self.pr_state()])
        self.loop(Commit("candidate"), APPROVE, Idle(""), provider=self.provider())
        with store.open(str(self.project.store_path)) as conn:
            run_id = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            note_id = send_back(conn, run_id, NOTE, "maintainer")
        self.push_log.unlink(missing_ok=True)
        self.serve(self.pr_state())
        title = "Apply the maintainer's instruction"
        self.loop(Commit(f"{title}\n\nPer operator_note event {note_id}: {NOTE}"),
                  Idle(""), provider=self.provider())

        _, tip = self.pushed()[-1]
        message = self.git("log", "-1", "--format=%B", tip).strip()
        self.assertEqual(
            message, f"{title}\n\nPer operator_note event {note_id}: {WITHHELD}")
