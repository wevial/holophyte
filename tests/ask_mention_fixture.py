"""Behavioral witnesses for explicit read-only pull request questions."""

import io
import json
import subprocess
from unittest.mock import patch

from fake_agent import APPROVE, Commit, Idle, Reply
from loop_fixture import BRANCH

from holophyte import agents, operator, pr, thread_mentions

# The factory's answer to an earlier ask in the same thread.
ANSWERED = "---- Comment by reviewer ----\n\nBecause guests are keyed by name."


class AskMentionCases:
    def test_mention_intents(self):
        for text, intent in [
            (" ? Can guests rename?", "ask"),
            (" AsK: Can guests rename?", "ask"),
            (" FiX: Can guests rename?", "fix"),
            (" Can guests rename?", "unmarked"),
        ]:
            with self.subTest(text=text):
                thread = pr.Thread(
                    "1", "app.py", 1, "maintainer", "  @HoLoPhYtE" + text + "  ", "url"
                )
                result = thread_mentions.classify(thread, "holophyte")
                self.assertEqual(result.classification, "MENTIONED")
                self.assertEqual(result.intent, intent)
                self.assertEqual(result.request, "Can guests rename?")

    def test_conversation_ask_pass(self):
        self.test_ask_pass(conversation=True)

    def test_latest_ask_overrides_earlier_fix(self):
        self.test_ask_pass(followup=True)

    def test_resolved_thread_follow_up_is_answered(self):
        self.test_ask_pass(resolved=True)

    def test_ask_pass(self, conversation=False, followup=False, resolved=False):
        self.configure(
            '[merge]\nmode = "pr"\napprove = "human"\n'
            '[example]\napi_key = "sentinel-ask-secret"\n'
        )
        question = (
            "@holophyte ask: Can an existing guest be renamed? sentinel-ask-secret"
        )
        comments = (question,)
        if followup:
            comments = ("@holophyte fix: Rename the guest",
                        ((("operator", "User"), question),))
        if resolved:
            comments = ("@holophyte ask: Why rename?",
                        ((("writer", "User"), ANSWERED),
                         (("operator", "User"), question)))
        state = (
            self.conversation_state(("operator", "User"), question)
            if conversation
            else self.pr_state(
                [("src/app.py", 30, ("operator", "User"), *comments)],
                mergeable="CONFLICTING",
            )
        )
        if resolved:
            self.review_threads(state)[0]["isResolved"] = True
        self.resume_with_conversation(
            state, initial_state=self.pr_state(checks="FAILURE")
        )
        sha = self.read("SELECT candidateSha FROM runs WHERE id = 1")[0][0]
        old_reason = self.read(
            "SELECT summary FROM runEvents WHERE runId = 1 "
            "AND summary LIKE '% -> awaiting_merge_approval:%'"
        )[0][0]
        calls_before = len(self.recorded())
        rounds = self.read("SELECT COUNT(*) FROM reviewRounds")[0][0]
        fake, _ = self.loop(
            Reply("Yes: src/app.py:30. sentinel-ask-secret A change is not warranted."),
            provider=self.provider(),
        )
        self.assertEqual(fake.roles, ["adjudicate"])
        self.assertIn("Can an existing guest be renamed?", fake.turns[0].goal)
        self.assertIn("Do not modify anything", fake.turns[0].goal)
        self.assertIn("Add a thing", fake.turns[0].goal)
        self.assertEqual(
            self.read("SELECT phase, candidateSha FROM runs ORDER BY id DESC")[0],
            ("awaiting_merge_approval", sha),
        )
        new_reason = self.read(
            "SELECT summary FROM runEvents "
            "WHERE summary LIKE '% -> awaiting_merge_approval:%' "
            "ORDER BY id DESC"
        )[0][0]
        self.assertEqual(new_reason.split(": ", 1)[1], old_reason.split(": ", 1)[1])
        self.assertEqual(self.read("SELECT COUNT(*) FROM reviewRounds")[0][0], rounds)
        self.assertEqual(
            self.read(
                "SELECT COUNT(*) FROM runs WHERE outcomeClass = 'work' "
                "AND outcome = 'failed'"
            )[0][0],
            0,
        )
        calls = self.api_calls()
        self.assertEqual(
            [k for k, _ in calls],
            (["state", "conversation"] if conversation
             else ["state", "reply", "resolve"]),
        )
        body = calls[1][1]["body"]
        self.assertIn("---- Comment by ", body)
        self.assertIn("src/app.py:30", body)
        self.assertNotIn("sentinel-ask-secret", body)
        self.assertIn("[redacted]", body)
        if conversation:
            self.assertIn("> @holophyte ask:", body)
        record = json.loads(
            self.read("SELECT summary FROM runEvents WHERE kind = 'instruction'")[0][0]
        )
        self.assertEqual(record["outcome"], "asked")
        self.assertIn(record["reply"], body)
        self.assertNotIn("sentinel-ask-secret", json.dumps(record))
        self.assertEqual(record["request"],
                         "Can an existing guest be renamed? [redacted]")
        self.assertEqual(record["line"], None if conversation else 30)
        self.assertFalse(any("push " in c for c in self.recorded()[calls_before:]))
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), sha)

        node = state["data"]["repository"]["pullRequest"]
        if conversation:
            node["comments"]["nodes"].append(
                self.comment(2, ("writer", "User"), body))
        else:
            review = node["reviewThreads"]["nodes"][0]
            review["comments"]["nodes"].append(
                self.comment(2, ("writer", "User"), body))
            review["isResolved"] = any(k == "resolve" for k, _ in calls)
            node["mergeable"] = "MERGEABLE"
        operator.babysit_ticket(self.tgt, "KO-131", operator.BABYSIT_DEFAULT_NOTE,
                                out=io.StringIO())
        self.serve(state)
        again, _ = self.loop(provider=self.provider())
        self.assertEqual(again.roles, [])
        self.assertEqual(len([k for k, _ in self.api_calls()
                              if k in ("reply", "conversation")]), 1)
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM runEvents WHERE kind = 'instruction'"), [(1,)])
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), sha)

    def test_mixed_mentions(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        threads = [
            ("src/app.py", 30, ("operator", "User"), "@holophyte ? Why?"),
            ("src/app.py", 40, ("operator", "User"), "@holophyte fix: Rename guest"),
        ]
        self.resume_with_conversation(self.pr_state(threads), self.pr_state())
        fake, _ = self.loop(
            Reply("Because src/app.py:30. No change warranted."),
            Commit("Rename guest"),
            Idle(""),
            provider=self.provider(),
        )
        self.assertEqual(fake.roles, ["adjudicate", "implement", "implement"])
        self.assertIn("Rename guest", fake.turns[1].goal)
        self.assertNotIn("Why?", fake.turns[1].goal)
        asked = json.loads(
            self.read("SELECT summary FROM runEvents WHERE kind = 'instruction'")[0][0]
        )
        self.assertEqual(asked["outcome"], "asked")
        findings = [
            f
            for (raw,) in self.read("SELECT findings FROM reviewRounds")
            for f in json.loads(raw)
            if f.get("kind") == "instruction"
        ]
        self.assertEqual([f["outcome"] for f in findings], ["changed"])

    def test_initial_auto_ask(self):
        self.configure('[merge]\nmode = "pr"\napprove = "auto"\n')
        self.fake_route(states=[self.pr_state([
            ("src/app.py", 30, ("operator", "User"), "@holophyte ask: Why?")])])
        fake, _ = self.loop(Commit("candidate"), APPROVE, Idle(""),
                            Reply("See src/app.py:30. No change warranted."),
                            provider=self.provider())
        self.assertEqual(fake.roles, ["implement", "review", "implement", "adjudicate"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual([k for k, _ in self.api_calls()],
                         ["state", "reply", "resolve", "merge"])

    def test_resolved_thread_without_new_mention_is_skipped(self):
        self.configure('[merge]\nmode = "pr"\napprove = "auto"\n')
        state = self.pr_state()
        self.review_threads(state).append(self.thread(
            1, "src/app.py", 30, ("operator", "User"), "@holophyte ask: Why?",
            ((("writer", "User"), ANSWERED),
             (("operator", "User"), "Thanks, that makes sense.")),
            resolved=True))
        self.fake_route(states=[state])
        fake, _ = self.loop(Commit("candidate"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual([k for k, _ in self.api_calls()], ["state", "merge"])

    @staticmethod
    def review_threads(state):
        return state["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]

    def failed_ask(self, result):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        state = self.conversation_state(("operator", "User"), "@holophyte ask: Why?")
        self.resume_with_conversation(state)
        sha = self.git("rev-parse", BRANCH).strip()

        class FailedAnswer:
            role = Reply.role

            def play(self, cwd, turn):
                kwargs = ({"side_effect": result} if isinstance(result, Exception)
                          else {"return_value": result})
                with patch("holophyte.agents.run_capped", **kwargs):
                    return agents.configured_review(
                        ["reviewer"], cwd, 1800, {}, "adjudicate", "reviewer")

        fake, _ = self.loop(FailedAnswer(), provider=self.provider())
        self.assertEqual(fake.roles, ["adjudicate"])
        self.assertEqual([k for k, _ in self.api_calls()], ["state"])
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM runEvents WHERE kind = 'instruction'"), [(0,)])
        self.assertEqual(self.read(
            "SELECT outcome, outcomeClass FROM runs ORDER BY id DESC LIMIT 1"),
            [("failed", "infra")])
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), sha)

    def test_nonzero_ask(self):
        self.failed_ask((1, "Could not read checkout"))

    def test_timed_out_ask(self):
        self.failed_ask(subprocess.TimeoutExpired("reviewer", 1800))

    def test_empty_ask(self):
        self.failed_ask((0, "  \n"))
