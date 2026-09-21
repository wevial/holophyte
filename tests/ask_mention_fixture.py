"""Behavioral witnesses for explicit read-only pull request questions."""

import json

from fake_agent import Commit, Idle, Reply
from loop_fixture import BRANCH

from holophyte import pr, thread_mentions


class AskMentionCases:
    def mention_intents(self):
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

    def conversation_ask_pass(self):
        self.ask_pass(conversation=True)

    def ask_pass(self, conversation=False):
        self.configure(
            '[merge]\nmode = "pr"\napprove = "human"\n'
            '[example]\napi_key = "sentinel-ask-secret"\n'
        )
        question = (
            "@holophyte ask: Can an existing guest be renamed? sentinel-ask-secret"
        )
        state = (
            self.conversation_state(("operator", "User"), question)
            if conversation
            else self.pr_state(
                [("src/app.py", 30, ("operator", "User"), question)],
                mergeable="CONFLICTING",
            )
        )
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
            ["state", "conversation" if conversation else "reply"],
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
        self.assertFalse(any("push " in c for c in self.recorded()[calls_before:]))
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), sha)

    def mixed_mentions(self):
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
