"""The `critic`, `pr_writer` and `babysit_reply` notes of `[pipeline.notes]`:
the critic's, the PR-text writer's and the thread-answer prompts each carry
their note in the notes block ahead of an unchanged tail, and a notes read
that fails leaves a pull request run as it is without notes.

Run: python3 -m unittest discover -s tests -p 'test_pipeline_notes_factory.py' -v
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit, Critic, Idle, Reply  # noqa: E402
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    VALID_BODY,
    MergeModeFixture,
    StubProvider,
    a_task,
)
from test_pipeline_notes import (  # noqa: E402 - after the sys.path insert above
    FRAME,
    GIT_DATES,
    block,
    notes_table,
)

from holophyte.agents.agent_routes import reset  # noqa: E402
from holophyte.stages import notes  # noqa: E402 - after the sys.path insert

NOTES = {"critic": "Treat a ticket naming a removed module as stale.",
         "pr_writer": "PR titles in our changelog style.",
         "babysit_reply": "Answer in two sentences at most."}
STYLE = "No ticket identifier in the title."
REPLY_FORMAT = ("Answer with exactly one line `TITLE: ...` (the title alone,"
                " under")
PROBE = f"#!{sys.executable}\nprint('ready')\n"
QUESTION = "@holophyte ask: Why is the thing added here?"
WRITTEN = Idle("TITLE: Add the thing\n\nThe thing is now added.\n")
ANSWER = Reply("Because the ticket asks for it: scripted-2.txt:1.")


class FactoryNotesRun(MergeModeFixture):
    """One pull request run: a critic-due ticket, a written PR text, one
    `ask` thread answered, then the merge."""

    def walked(self, extra):
        probe = self.target.parent / "critic-probe"
        probe.write_text(PROBE)
        probe.chmod(0o755)
        self.configure(f'[merge]\nmode = "pr"\npr_style = "{STYLE}"\n'
                       f'[agents.critic]\n[harnesses]\ncodex = "{probe}"\n'
                       '[loop]\ncritic_after_hours = 12\n[trim]\n'
                       'enabled = false\n' + extra)
        self.addCleanup(reset, self.project)
        self.fake_route(states=[
            self.pr_state([("src/app.py", 30, ("operator", "User"), QUESTION)]),
            self.pr_state()])
        filed = int(time.time() * 1000) - 13 * 3600 * 1000
        provider = StubProvider(dict(a_task(), body=VALID_BODY, filed_at=filed))
        with patch.dict(os.environ, GIT_DATES):
            fake, _ = self.loop(Critic(), Commit("the scripted work"), APPROVE,
                                WRITTEN, ANSWER, provider=provider)
        goals = {turn.role: turn.goal for turn in fake.turns}
        prompts = {"critic": goals["critic"], "babysit_reply": goals["adjudicate"],
                   "pr_writer": next(turn.goal for turn in fake.turns
                                     if REPLY_FORMAT in turn.goal)}
        phases = self.read("SELECT summary FROM runEvents"
                           " WHERE kind = 'phase_change' ORDER BY seq")
        ledger = [(kind, re.sub(r"\d+(\.\d+)? ?min", "N min", text))
                  for kind, text in self.read(
                      "SELECT kind, text FROM ledger ORDER BY id")]
        return (prompts, fake.roles, phases, ledger,
                self.read("SELECT outcome FROM runs"))

    def fresh(self):
        self.doCleanups()
        self.setUp()


class FactoryNotesPromptTests(FactoryNotesRun):
    def test_each_factory_prompt_holds_its_note_ahead_of_an_unchanged_tail(self):
        plain = self.walked("")[0]
        self.fresh()
        noted, roles, _, _, outcome = self.walked(notes_table(**NOTES))

        self.assertEqual(outcome, [("merged",)])
        self.assertEqual(roles, ["critic", "implement", "review", "implement",
                                 "adjudicate"])
        tails = {"critic": "End your reply with exactly one of these lines",
                 "pr_writer": "The ticket:\n\n",
                 "babysit_reply": "Ticket:\n"}
        for target, tail in tails.items():
            with self.subTest(target=target):
                text, prompt = NOTES[target], noted[target]
                self.assertNotIn(FRAME, plain[target])
                start = prompt.index(block(text))
                self.assertEqual(prompt[:start] + prompt[start + len(block(text)):],
                                 plain[target])
                self.assertTrue(plain[target][start:].startswith(tail),
                                plain[target][start:start + 80])
        writer = noted["pr_writer"]
        order = [writer.index(STYLE), writer.index(block(NOTES["pr_writer"])),
                 writer.index("The thing, added."), writer.index("```diff\n")]
        self.assertEqual(order, sorted(order))


class FailedFactoryNotesTests(FactoryNotesRun):
    def test_a_notes_read_that_raises_leaves_the_pull_request_run_unchanged(self):
        plain = self.walked("")
        self.fresh()
        refused = RuntimeError("notes unreadable")
        with patch.object(notes, "block", side_effect=refused) as read:
            broken = self.walked(notes_table(**NOTES))

        self.assertEqual(plain[4], [("merged",)])
        self.assertEqual(broken, plain)
        self.assertEqual([call.args[3] for call in read.call_args_list],
                         ["critic", "implement", "review", "pr_writer",
                          "babysit_reply"])
        writes = [json.loads(payload)["write"] for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'stage_record_failed'")]
        self.assertEqual(writes, ["pipeline_notes"] * 4)


if __name__ == "__main__":
    unittest.main()
