"""A done scout steered through `POST /actions/scout-message`, `holo scout
steer` and the `holo mcp` `scout_steer` tool, and the proposal cards an
answer turn or a pass posts, answered with a message naming the card; each
over a real git repository and store, the scout sweep's spawn a recorder or
the lead a stub CLI.

Run: python3 -m unittest discover -s tests -p 'test_scout_proposals.py' -v
"""
import contextlib
import io
import json
import os
import threading
from unittest.mock import patch

import store
from holophyte.scout.answer import answer, current_ask, settle
from holophyte.scout.follow_up import current_pass, run_pass, settle_pass
from holophyte.scout.proposals import MAX_PROPOSALS
from holophyte.scout.scout_process import make_lead
from holophyte.scout.swarm import Stopping
from holophyte.scout.views import scout_detail
from store.scout_thread import ThreadRefused, messages, post_message, take_message
from store.scouts import (
    create_scout,
    end_scout,
    move_scout,
    record_scout_report,
    scout,
)
from tests.scout_stubs import fenced
from tests.test_holo_mcp import McpCase
from tests.test_scout_answer import AnswerCase
from tests.test_scout_follow_up import QUESTIONS, SECOND, PassCase, plan
from tests.test_scout_launch import slots
from tests.test_scout_thread_action import ActionCase, done_scout, live_scout
from tests.test_store_scout_thread import ThreadCase

STEER = "Go check the digest email as well"
FOLD = "Fold F5 into A1, or file it as A5?"
CHOICES = [{"label": "Fold into A1", "mode": "steer"},
           {"label": "File it as A5", "mode": "ask"}]
PROPOSALS = [{"question": FOLD, "choices": CHOICES},
             {"question": "Check the cron too?",
              "choices": [{"label": "Yes", "mode": "steer"}]},
             {"question": "Is the digest daily?",
              "choices": [{"label": "Daily", "mode": "ask"},
                          {"label": "Weekly", "mode": "ask"},
                          {"label": "Ask the owner", "mode": "steer"}]}]


def card(conn, scout_id, question=FOLD, choices=CHOICES, answer_text=None):
    """A done scout's ask taken, optionally answered, then a card answering
    it; the scout back in `done`. Returns the ask's, answer's and card's ids."""
    asked = post_message(conn, scout_id, "maintainer", "ask", "What next?")
    take_message(conn, asked)
    answered = None if answer_text is None else post_message(
        conn, scout_id, "scout", "answer", answer_text, answers_id=asked)
    posted = post_message(conn, scout_id, "scout", "answer", question,
                          blocks=[{"type": "proposal", "question": question,
                                   "choices": choices}], answers_id=asked)
    move_scout(conn, scout_id, "done")
    return asked, answered, posted


def reported_scout(conn):
    (project,) = conn.execute("SELECT id FROM projects").fetchone()
    scout_id = create_scout(conn, project, "where is the bug?", "research", 30)
    move_scout(conn, scout_id, "planning", base_sha="a" * 40)
    record_scout_report(conn, scout_id, 1, {"summary": "pass one",
                                            "findings": [], "actions": []})
    end_scout(conn, scout_id, "done", "the lead wrote its report")
    return scout_id


class DaemonSteerTests(ActionCase):
    def test_a_steer_starts_pass_two_and_a_second_waits_behind_the_first(self):
        path = self.project("alpha")
        scout_id = reported_scout(self.conn(path))
        server = self.serve(path)
        code, reply = self.post(server, "/actions/scout-message",
                                {"scout": scout_id, "mode": "steer",
                                 "text": STEER})
        self.assertEqual(code, 200, reply)
        self.assertEqual((reply["ok"], reply["queued"]), (True, False))
        self.assertIn("pass 2", reply["detail"])
        (steer,) = self.thread(path, scout_id)
        self.assertEqual((steer.id, steer.mode, steer.text),
                         (reply["message"], "steer", STEER))
        (argv, phase, _), = self.spawned
        self.assertEqual((argv[-2], phase), (str(scout_id), "planning"))

        slots(1)
        waiting = reported_scout(self.conn(path))
        code, first = self.post(server, "/actions/scout-message",
                                {"scout": waiting, "mode": "steer",
                                 "text": STEER})
        self.assertEqual((code, first["ok"], first["queued"]),
                         (200, True, True), first)
        code, second = self.post(server, "/actions/scout-message",
                                 {"scout": waiting, "mode": "steer",
                                  "text": "And the cron"})
        self.assertEqual((code, second["ok"]), (200, False), second)
        self.assertIn(f"message {first['message']}", second["detail"])
        self.assertEqual([message.text for message in
                          self.thread(path, waiting)], [STEER])

    def test_a_message_answering_anything_but_an_untaken_card_is_refused(self):
        path = self.project("alpha")
        conn = self.conn(path)
        scout_id = done_scout(conn)
        asked, answered, open_card = card(conn, scout_id,
                                          answer_text="It spins.")
        _, _, taken_card = card(conn, scout_id, "Check the cron?")
        take_message(conn, post_message(conn, scout_id, "maintainer", "ask",
                                        "Yes", answers_id=taken_card))
        move_scout(conn, scout_id, "done")
        _, _, other_card = card(conn, done_scout(conn))
        server = self.serve(path)
        before = self.thread(path, scout_id)

        for given in ("3", None):
            with self.subTest(answers=given):
                code, reply = self.post(server, "/actions/scout-message",
                                        {"scout": scout_id, "mode": "ask",
                                         "text": "Fold into A1",
                                         "answers": given})
                self.assertEqual(code, 400, reply)
                self.assertIn("answers", reply["error"])
        for named in (asked, answered, other_card, taken_card, 2**63):
            with self.subTest(answers=named):
                code, reply = self.post(server, "/actions/scout-message",
                                        {"scout": scout_id, "mode": "ask",
                                         "text": "Fold into A1",
                                         "answers": named})
                self.assertEqual((code, reply["ok"], reply["reason"]),
                                 (200, False, "card_refused"), reply)
                self.assertIn(f"message {named}", reply["detail"])
        self.assertEqual(self.thread(path, scout_id), before)
        self.assertEqual(self.spawned, [])

        code, reply = self.post(server, "/actions/scout-message",
                                {"scout": scout_id, "mode": "ask",
                                 "text": "Fold into A1", "answers": open_card})
        self.assertEqual((code, reply["ok"]), (200, True), reply)
        self.assertEqual(self.thread(path, scout_id)[-1].answers_id, open_card)


class CardRaceTests(ThreadCase):
    def test_two_connections_answering_one_card_answer_it_once(self):
        scout_id = self.scout_in("done")
        _, _, posted = card(self.conn, scout_id)
        start, outcomes = threading.Barrier(2), {}

        def answer_card(n):
            conn = store.open(self.path)
            try:
                start.wait()
                outcomes[n] = post_message(conn, scout_id, "maintainer",
                                           "steer", f"choice {n}",
                                           answers_id=posted)
            except Exception as exc:  # noqa: BLE001 - the loser's error is asserted
                outcomes[n] = exc
            finally:
                conn.close()

        posters = [threading.Thread(target=answer_card, args=(n,))
                   for n in range(2)]
        for poster in posters:
            poster.start()
        for poster in posters:
            poster.join(timeout=30)

        self.assertEqual(len(outcomes), 2, outcomes)
        refused = [out for out in outcomes.values()
                   if isinstance(out, Exception)]
        self.assertEqual(len(refused), 1, outcomes)
        self.assertIsInstance(refused[0], ThreadRefused)
        self.assertIn(f"message {posted}", str(refused[0]))
        self.assertEqual(
            [message.id for message in messages(self.conn, scout_id)
             if message.answers_id == posted and message.author != "scout"],
            [out for out in outcomes.values() if out not in refused])


class AnswerCardTests(AnswerCase):
    def test_an_answer_posts_a_card_per_valid_proposal_and_names_the_rest(self):
        self.stubs.answer(answer=fenced({"answer": "A1 fixes it.", "refs": [],
                                         "proposals": PROPOSALS}))
        scout_id = self.answering()
        (asked,) = messages(self.store_conn, scout_id)
        self.assertEqual(self.answer_step(scout_id), 0)

        _, said, *cards = messages(self.store_conn, scout_id)
        self.assertEqual((said.mode, said.text), ("answer", "A1 fixes it."))
        kept = [PROPOSALS[0], PROPOSALS[2]]
        self.assertEqual(
            [(posted.author, posted.mode, posted.answers_id, posted.text,
              posted.blocks) for posted in cards],
            [("scout", "answer", asked.id, proposal["question"],
              [{"type": "proposal", **proposal}]) for proposal in kept])
        (payload,) = self.payloads(scout_id, "scout_answered")
        (dropped,) = payload["dropped_proposals"]
        self.assertTrue(dropped.startswith("proposal 2:"), dropped)
        self.assertEqual(scout(self.store_conn, scout_id).phase, "done")


class PassCardTests(PassCase):
    def run_steered(self, scout_id):
        row = scout(self.store_conn, scout_id)
        worktree = make_lead(self.target, scout_id, row.base_sha)
        outcome = run_pass(self.store_conn, self.target, row,
                           current_pass(self.store_conn, scout_id), worktree,
                           60, Stopping())
        return settle_pass(self.store_conn, scout_id, outcome)

    def test_a_pass_posts_at_most_the_cap_of_cards_and_names_the_rest(self):
        proposals = [PROPOSALS[0], PROPOSALS[2],
                     {"question": "One more?", "choices": CHOICES}]
        self.assertGreater(len(proposals), MAX_PROPOSALS)
        self.stubs.answer(pass_plan=plan(QUESTIONS[0]),
                          report=fenced(dict(SECOND, proposals=proposals)))
        scout_id = self.steered()
        (steer,) = messages(self.store_conn, scout_id)
        self.assertEqual(self.run_steered(scout_id), 0)

        _, *cards = messages(self.store_conn, scout_id)
        self.assertEqual([(posted.text, posted.answers_id) for posted in cards],
                         [(proposal["question"], steer.id) for proposal
                          in proposals[:MAX_PROPOSALS]])
        (payload,) = self.payloads(scout_id, "scout_pass_done")
        self.assertEqual([name.split(":")[0] for name in
                          payload["dropped_proposals"]],
                         [f"proposal {number}" for number in
                          range(MAX_PROPOSALS + 1, len(proposals) + 1)])
        code, body = scout_detail(self.target, str(scout_id))
        self.assertEqual(code, 200, body)
        served = {item["pass"]: item["report"] for item in body["reports"]}
        self.assertEqual(served[2]["summary"], SECOND["summary"])
        self.assertNotIn("proposals", served[2])

    def test_the_turn_taking_a_cards_answer_reads_the_cards_question(self):
        self.stubs.answer(pass_plan=plan(QUESTIONS[0]), report=fenced(SECOND),
                          answer=fenced({"answer": "Daily.", "refs": []}))
        scout_id = self.done_scout()
        _, _, folded = card(self.store_conn, scout_id)
        take_message(self.store_conn, post_message(
            self.store_conn, scout_id, "maintainer", "steer", "Fold into A1",
            answers_id=folded))
        self.assertEqual(self.run_steered(scout_id), 0)
        (planned,) = self.stubs.turns("pass_plan")
        self.assertIn(FOLD, planned["argv"][-1])
        self.assertIn("Fold into A1", planned["argv"][-1])

        _, _, daily = card(self.store_conn, scout_id, PROPOSALS[2]["question"],
                           PROPOSALS[2]["choices"])
        take_message(self.store_conn, post_message(
            self.store_conn, scout_id, "maintainer", "ask", "Daily",
            answers_id=daily))
        row = scout(self.store_conn, scout_id)
        outcome = answer(self.store_conn, self.target, row,
                         current_ask(self.store_conn, scout_id),
                         make_lead(self.target, scout_id, row.base_sha),
                         Stopping())
        self.assertEqual(settle(self.store_conn, scout_id, outcome), 0)
        (answered,) = self.stubs.turns("answer")
        self.assertIn(PROPOSALS[2]["question"], answered["argv"][-1])


class HoloSteerTests(ActionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"HOLO_TRANSPORT": "local"}))

    def holo(self, *argv):
        from holophyte.holo.cli import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(argv))
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_holo_scout_steer_ask_answers_and_thread_show_the_card(self):
        path = self.project("alpha")
        scout_id = reported_scout(self.conn(path))
        code, _, err = self.holo("scout", "steer", str(scout_id), "-p",
                                 str(path))
        self.assertEqual(code, 2, err)
        self.assertEqual(self.thread(path, scout_id), [])
        code, out, err = self.holo("scout", "steer", str(scout_id), STEER,
                                   "--json", "-p", str(path))
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result["ok"], result["action"]),
                         (True, "scout steer"))
        (steer,) = self.thread(path, scout_id)
        self.assertEqual((steer.mode, steer.text), ("steer", STEER))

        carded = done_scout(self.conn(path))
        _, _, posted = card(self.conn(path), carded)
        code, out, err = self.holo("scout", "ask", str(carded), "Fold into A1",
                                   "--answers", str(posted), "--json", "-p",
                                   str(path))
        self.assertEqual(code, 0, err)
        self.assertIs(json.loads(out)["ok"], True)
        asked = self.thread(path, carded)[-1]
        self.assertEqual((asked.mode, asked.text, asked.answers_id),
                         ("ask", "Fold into A1", posted))

        code, out, err = self.holo("scout", "thread", str(carded), "-p",
                                   str(path))
        self.assertEqual(code, 0, err)
        self.assertIn(FOLD, out)
        for choice in CHOICES:
            (line,) = [line for line in out.splitlines()
                       if choice["label"] in line and "message" not in line]
            self.assertIn(choice["mode"], line)


class McpSteerTests(McpCase):
    def test_the_scout_steer_tool_records_a_signed_steer_answering_a_card(self):
        conn = store.open(str(self.store_path))
        try:
            live_scout(conn)
            scout_id = done_scout(conn)
            _, _, posted = card(conn, scout_id)
        finally:
            conn.close()
        slots(1)

        async def use(client, _):
            return (await client.list_tools()).tools
        tools = {tool.name: tool for tool in self.session(use)}
        self.assertLessEqual({"scout", "note", "author"},
                             set(tools["scout_steer"].input_schema["required"]))
        for name in ("scout_steer", "scout_ask"):
            self.assertIn("answers", tools[name].input_schema["properties"])
        result = self.call("scout_steer", {
            "project": "alpha", "scout": scout_id, "note": "Go on",
            "author": "test seat", "answers": posted})
        self.assertIs(result.is_error, False, result.content)
        self.assertIs(result.structured_content["ok"], True)
        conn = store.open(str(self.store_path))
        try:
            steer = messages(conn, scout_id)[-1]
        finally:
            conn.close()
        self.assertEqual((steer.mode, steer.text, steer.author,
                          steer.answers_id, steer.consumed_by),
                         ("steer", "Go on", "test seat via MCP", posted, None))
