"""KO-608: a paused run's pull request carries the `holophyte:paused` label
and no comment, so the pause note never reaches GitHub; `--resume` unlabels."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # factory.py imports store/ticket_template by name
sys.path.insert(0, str(HERE))  # fake_agent and the fixtures live beside this file
from fake_agent import APPROVE, Commit, Idle, Reply  # noqa: E402
from loop_fixture import MergeModeFixture  # noqa: E402
from pause_fixture import PauseEdit  # noqa: E402

from holophyte.loop.stop import command  # noqa: E402

LABELS = "repos/example/repo/issues/7/labels"


class PauseNoticeTests(MergeModeFixture):
    def pause_in_babysit(self, **route):
        """Pause a babysit pass's fix turn on an open pull request."""
        self.configure('[merge]\nmode = "pr"\napprove = "auto"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT])], **route)
        self.loop(Commit(), APPROVE, Idle(''),
                  Reply('THREAD 1: ADDRESS -- broken'), PauseEdit(self.db),
                  provider=self.provider())

    def notices(self):
        return [json.loads(payload) for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'pause_notice' ORDER BY seq")]

    def github(self):
        """The label and comment calls the recording `gh` received."""
        return [line for line in self.recorded()
                if "/labels" in line or "/comments" in line]

    def test_pause_labels_without_comment_or_note_then_resume_unlabels(self):
        self.pause_in_babysit()
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("paused",)])
        paused = self.github()
        self.assertEqual(len(paused), 1, paused)
        self.assertIn(f"--method POST {LABELS}", paused[0])
        self.assertEqual(json.loads(self.label_log.read_text()),
                         {"labels": ["holophyte:paused"]})
        self.assertNotIn("conversation", [kind for kind, _ in self.api_calls()])
        wire = [*self.recorded(), self.label_log.read_text(),
                *(json.dumps(body) for _, body in self.api_calls())]
        self.assertFalse([call for call in wire if "reboot writer" in call])

        command(self.project, "KO-131", None, resume=True)
        after = self.github()[len(paused):]
        self.assertEqual(len(after), 1, after)
        self.assertIn(f"--method DELETE {LABELS}/holophyte%3Apaused", after[0])
        self.assertEqual(self.read("SELECT status FROM tickets"), [("ready",)])

    def test_refused_label_leaves_the_pause_standing(self):
        self.pause_in_babysit(refuse_labels=True)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("paused",)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("blocked_on_operator",)])
        self.assertIn("label", [n.get("step") for n in self.notices()])

    def test_run_without_pull_request_posts_nothing(self):
        self.fake_route()
        self.loop(PauseEdit(self.db))
        self.assertEqual(self.read("SELECT outcome, prUrl FROM runs"),
                         [("paused", None)])
        command(self.project, "KO-131", None, resume=True)
        self.assertEqual(self.github(), [])
        self.assertEqual(self.notices(), [])


if __name__ == "__main__":
    unittest.main()
