"""Babysit mentions, reactions and declined threads under `[merge] mode = "pr"`."""
from __future__ import annotations

import json
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
# Resolve the harness identically under discovery and named unittest modules.
sys.path.insert(0, str(HERE))
import babysit_fixture as cases  # noqa: E402
from ask_mention_fixture import AskMentionCases  # noqa: E402
from bot_thread_fixture import BotThreadCases  # noqa: E402
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    Commit,
    Idle,
    Reply,
)
from loop_fixture import MergeModeFixture  # noqa: E402
from mention_accounts_fixture import MentionAccountCases  # noqa: E402
from triage_mention_fixture import TriageMentionCases  # noqa: E402

import holophyte.agents.agent_output  # noqa: E402 - after the sys.path insert above
import holophyte.agents.roles  # noqa: E402 - after the sys.path insert above
import holophyte.cli.operator  # noqa: E402 - after the sys.path insert above
import holophyte.loop.gates  # noqa: E402 - after the sys.path insert above
import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pr_status  # noqa: E402 - after the sys.path insert above
from holophyte.babysit.thread_mentions import REFUSAL  # noqa: E402


@dataclass
class SeesCalls(Commit):
    """A fix turn that notes the GitHub calls already made when it began."""

    fixture: object = None
    seen: list = field(default_factory=list)

    def play(self, cwd, turn):
        self.seen.extend(kind for kind, _ in self.fixture.api_calls())
        return super().play(cwd, turn)


class MergeModeBabysitMentionTests(MentionAccountCases, TriageMentionCases,
                                   AskMentionCases,
                                   cases.OperatorNoteCase,
                                   BotThreadCases, cases.BabysitHelpers,
                                   MergeModeFixture):
    """Mention routing, instructions, reactions, bot policy and operator notes."""
    def test_human_conversation_mention_is_fixed_and_replied_on_the_pull(self):
        self.human_conversation_mention_is_fixed_and_replied_on_the_pull()

    def test_bot_conversation_mentions_and_unmentioned_humans_are_ignored(self):
        self.bot_conversation_mentions_and_unmentioned_humans_are_ignored()

    def test_two_instructions_are_stored_with_posted_outcomes(self):
        self.configure('[merge]\nmode = "pr"\n')
        threads = [("src/app.py", 30, ("operator", "User"),
                    "@holophyte fix: use the path token\n  and reject missing tokens"),
                   ("src/app.py", 40, ("maintainer", "User"),
                    "@holophyte fix: preserve validation")]
        self.fake_route(states=[self.pr_state(threads), self.pr_state()])
        pending = []
        original = holophyte.pr.github.reply_thread

        def reply(*args, **kwargs):
            pending.extend(json.loads(self.read(
                "SELECT findings FROM reviewRounds WHERE round = 2")[0][0]))
            return original(*args, **kwargs)

        with patch("holophyte.pr.github.reply_thread", side_effect=reply):
            fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                                Commit("fix tokens"), APPROVE, Idle(""),
                                provider=self.provider())
        findings = json.loads(self.read(
            "SELECT findings FROM reviewRounds WHERE round = 2")[0][0])
        self.assertEqual(len(findings), 2)
        replies = [data["body"] for kind, data in self.api_calls() if kind == "reply"]
        for index, (finding, request, author) in enumerate(zip(
                findings, ("use the path token\n  and reject missing tokens",
                           "preserve validation"),
                ("operator", "maintainer"))):
            self.assertEqual({k: finding[k] for k in
                              ("kind", "path", "line", "author", "request", "url")},
                             dict(kind="instruction", path="src/app.py",
                                  line=30 + index * 10, author=author, request=request,
                                  url=self.URL + f"#discussion_r{index + 1}"))
            self.assertEqual(finding["outcome"], "changed")
            self.assertEqual(finding["reply"], replies[index])
        self.assertNotIn("outcome", pending[0])
        self.assertNotIn("outcome", pending[1])
        self.assertIn("ADDRESS: use the path token and reject missing tokens",
                      fake.turns[-1].goal)

    def test_configured_user_bots_are_stored_as_findings_without_outcomes(self):
        self.configure('[merge]\nmode = "pr"\nbot_logins = ["SERVICE"]\n'
                       'bot_authors = ["REVIEWER"]\n')
        authors = ("service", "Reviewer", "automation[BOT]")
        threads = [("src/app.py", 30, (author, "User"),
                    "@holophyte fix: fix tokens") for author in authors]
        threads.append(("src/app.py", 40, ("service", "User"), "Which token?",
                        ((("operator", "User"),
                          "@holophyte fix: preserve validation"),)))
        self.fake_route(states=[self.pr_state(threads), self.pr_state()])
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  Commit("fix tokens"), APPROVE, Idle(""), provider=self.provider())
        findings = json.loads(self.read(
            "SELECT findings FROM reviewRounds WHERE round = 2")[0][0])
        self.assertEqual(len(findings), 4)
        for finding, author in zip(findings, authors):
            self.assertEqual(finding["kind"], "thread")
            self.assertEqual(finding["author"], author)
            self.assertNotIn("outcome", finding)
        self.assertEqual(findings[-1]["kind"], "instruction")
        self.assertEqual(findings[-1]["author"], "operator")
        self.assertEqual(findings[-1]["outcome"], "changed")

    def test_bot_mention_is_a_finding_but_latest_human_mention_is_instruction(self):
        self.configure('[merge]\nmode = "pr"\n')
        threads = [
            ("src/app.py", 30, ("coderabbitai", "Bot"),
             "<details>In `@holophyte/app.py` handle empty tokens</details>\n"
             "**Handle empty tokens.**\nPreserve validation."),
            ("src/app.py", 40, ("coderabbitai", "Bot"), "Which token?",
             ((("operator", "User"), "@holophyte fix: preserve validation"),)),
        ]
        self.fake_route(states=[self.pr_state(threads), self.pr_state()])
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  Commit("fix tokens"), APPROVE, Idle(""), provider=self.provider())
        findings = json.loads(self.read(
            "SELECT findings FROM reviewRounds WHERE round = 2")[0][0])
        self.assertEqual(len(findings), 2)
        bot, person = findings
        self.assertNotEqual(bot.get("kind"), "instruction")
        self.assertEqual(bot["author"], "coderabbitai")
        self.assertIn("**Handle empty tokens.**\nPreserve validation.", bot["message"])
        self.assertEqual(person["kind"], "instruction")
        self.assertEqual(person["author"], "operator")
        self.assertEqual(person["request"], "preserve validation")

    def test_unmentioned_latest_reply_is_judged_with_whole_conversation(self):
        self.configure('[merge]\nmode = "pr"\nhuman_threads = "act"\n')
        threads = [
            ("src/app.py", 30, ("reviewer", "User"), "Which token?",
             ((("operator", "User"), "Use the path tokenId"),)),
            ("src/app.py", 40, ("reviewer", "User"), "@holophyte fix: rename it",
             ((("operator", "User"), "Which name should we use?"),)),
        ]
        self.fake_route(states=[self.pr_state(threads)])
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- use path tokenId\n"
                                  "THREAD 2: HUMAN -- a question"),
                            Commit("fix: use path token"), provider=self.provider())
        goal = fake.turns[3].goal
        self.assertLess(goal.index("@reviewer: Which token?"),
                        goal.index("@operator: Use the path tokenId"))
        self.assertIn("@reviewer: @holophyte fix: rename it", goal)
        self.assertIn("@operator: Which name should we use?", goal)
        self.assertIn("a concrete change stated by a later reply "
                      "is the thread's request", goal)
        self.assertIn("use path tokenId", fake.turns[4].goal)
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state", "reply"])
        findings = json.loads(self.read(
            "SELECT findings FROM reviewRounds WHERE round = 2")[0][0])
        self.assertEqual(len(findings), 2)
        for finding in findings:
            with self.subTest(message=finding["message"]):
                self.assertNotIn("VERDICT:", finding["message"])

    def test_custom_handle_routes_only_its_exact_latest_mention(self):
        self.configure('[merge]\nmode = "pr"\nhuman_threads = "act"\n'
                       'mention_handle = "factory-bot"\n')
        threads = [("src/app.py", 30, ("operator", "User"),
                    "@factory-bot fix: use path tokenId"),
                   ("src/app.py", 40, ("reviewer", "User"),
                    "@holophyte @factory-bot-extra @factory-bot2 which token?")]
        self.fake_route(states=[self.pr_state(threads)])
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: HUMAN -- a question"),
                            Commit("fix: use path token"), provider=self.provider())
        self.assertNotIn("use path tokenId", fake.turns[3].goal)
        self.assertIn(threads[1][3], fake.turns[3].goal)
        self.assertIn("Instruction from @operator on the pull request:\n"
                      "use path tokenId",
                      fake.turns[4].goal)
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "reply", "resolve"])
        self.assertIn("needs a human's answer", self.question())

    def with_node_ids(self, state, eyes=()):
        """`state` with each review comment's GraphQL id, `C_` and its URL's
        number, and the route's own EYES reaction on the ids in `eyes`."""
        pull = state["data"]["repository"]["pullRequest"]
        for t in pull["reviewThreads"]["nodes"]:
            for c in t["comments"]["nodes"]:
                c["id"] = "C_" + c["url"].rsplit("_r", 1)[1]
                if c["id"] in eyes:
                    c["reactionGroups"] = [
                        {"content": "THUMBS_UP", "viewerHasReacted": True},
                        {"content": "EYES", "viewerHasReacted": True}]
        return state

    def reactions(self):
        """The subject of each `addReaction` sent, asserting it was EYES."""
        subjects = []
        for path in sorted(self.api_dir.iterdir(), key=lambda p: int(p.stem)):
            body = json.loads(path.read_text())
            if "addReaction" in body.get("query", ""):
                self.assertIn("content: EYES", body["query"])
                subjects.append(body["variables"]["subject"])
        return subjects

    def mention_fixed(self, thread, eyes=(), refuse_reactions=False, config=""):
        self.configure('[merge]\nmode = "pr"\n' + config)
        self.fake_route(states=[self.with_node_ids(self.pr_state([thread]), eyes),
                                self.pr_state()],
                        refuse_reactions=refuse_reactions)
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: use path token"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertTrue(any("use the path tokenId" in t.goal for t in fake.turns))
        replies = [data for kind, data in self.api_calls() if kind == "reply"]
        self.assertEqual(len(replies), 1)
        self.assertIn("Addressed in ", replies[0]["body"])
        return self.reactions()

    def test_a_conversation_mention_is_acknowledged_once_before_its_fix(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        state = self.conversation_state(("operator", "User"),
                                        "@holophyte fix: move the button")
        self.resume_with_conversation(state, self.pr_state())
        fix = SeesCalls("fix: move button", fixture=self)
        fake, _ = self.loop(fix, Idle(""), provider=self.provider())
        self.assertEqual(fake.roles, ["implement", "implement"])
        self.assertEqual(self.reactions(), ["IC_1"])
        self.assertEqual(fix.seen, ["state", "react"])

    def test_a_review_mention_in_a_later_reply_is_acknowledged_on_it(self):
        thread = ("src/app.py", 30, ("reviewer", "User"), "Which token?",
                  ((("reviewer", "User"), "The guest one, @holophyte?"),
                   (("operator", "User"),
                    "@holophyte fix: use the path tokenId")))
        self.assertEqual(self.mention_fixed(thread), ["C_1_2"])

    def test_a_mention_the_factory_already_acknowledged_is_not_reacted_to(self):
        thread = ("src/app.py", 30, ("reviewer", "User"), "Which token?",
                  ((("reviewer", "User"), "The guest one?"),
                   (("operator", "User"),
                    "@holophyte fix: use the path tokenId")))
        self.assertEqual(self.mention_fixed(thread, eyes=("C_1_2",)), [])

    def test_a_configured_bot_s_mention_gets_no_reaction(self):
        thread = ("src/app.py", 30, ("service", "User"),
                  "@holophyte fix: use the path tokenId")
        self.assertEqual(self.mention_fixed(
            thread, config='bot_logins = ["service"]\n'), [])

    def test_a_refused_reaction_is_a_run_event_and_the_fix_still_runs(self):
        thread = ("src/app.py", 30, ("operator", "User"),
                  "@holophyte fix: use the path tokenId")
        self.assertEqual(self.mention_fixed(thread, refuse_reactions=True),
                         ["C_1"])
        (summary,), = self.read("SELECT summary FROM runEvents"
                                " WHERE summary LIKE '%EYES reaction%'")
        self.assertIn(f"{self.URL}#discussion_r1", summary)
        self.assertIn("reaction refused", summary)

    def test_a_mention_from_an_unlisted_account_gets_no_reaction(self):
        self.configure('[merge]\nmode = "pr"\nmention_accounts = ["operator"]\n')
        thread = ("src/app.py", 30, ("stranger", "User"),
                  "@holophyte fix: change tokens")
        self.fake_route(states=[self.with_node_ids(self.pr_state([thread]))])
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  provider=self.provider())
        replies = [data["body"] for kind, data in self.api_calls()
                   if kind == "reply"]
        self.assertTrue(replies)
        self.assertTrue(all(REFUSAL in body for body in replies))
        self.assertEqual(self.reactions(), [])

    def declined_thread(self, author, config=""):
        self.configure('[merge]\nmode = "pr"\n' + config)
        thread = (*self.NIT[:2], author, self.NIT[3])
        self.fake_route(states=[self.pr_state([thread]), self.pr_state()])
        out = self.main_output(Commit("the scripted work"), APPROVE, Idle(""),
                               Reply("THREAD 1: DECLINE -- a naming preference"),
                               provider=self.provider())
        calls = self.api_calls()
        self.assertEqual(calls[1][0], "reply")
        self.assertEqual(calls[1][1]["thread"], "PRRT_1")
        self.assertIn("Declined: a naming preference", calls[1][1]["body"])
        return out, calls

    def test_declined_listed_bot_is_resolved_and_merges(self):
        out, calls = self.declined_thread("devin-ai-integration")
        self.assertEqual([kind for kind, _ in calls],
                         ["state", "reply", "resolve", "state", "merge"])
        self.assertEqual(calls[2][1], {"thread": "PRRT_1"})
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])
        self.assertIn("1 thread(s) declined; 1 from bots resolved with the reason", out)
        self.assertNotIn("Leaving this thread open", calls[1][1]["body"])

    def test_declined_bot_suffix_is_resolved_without_a_list_entry(self):
        _, calls = self.declined_thread("unlisted[bot]", "bot_authors = []\n")
        self.assertEqual([kind for kind, _ in calls],
                         ["state", "reply", "resolve", "state", "merge"])
        self.assertEqual(calls[2][1], {"thread": "PRRT_1"})
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_declined_human_is_replied_to_left_open_and_parks(self):
        with patch("holophyte.babysit.babysitter._verdicts_by_kind",
                   return_value={1: ("DECLINE", "a naming preference")}):
            _, calls = self.declined_thread(("alice", "User"))
        self.assertEqual([kind for kind, _ in calls], ["state", "reply"])
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertIn("1 thread(s) declined and left open", self.question())
        self.assertIn("@alice", self.question())


if __name__ == "__main__":
    unittest.main()
