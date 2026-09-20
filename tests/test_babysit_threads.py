"""`holophyte.babysitter`'s pass under `[merge] mode = "pr"`, end to end."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
# Discovery never imports `fake_agent`; put its `tests/` directory on the path.
# Putting it there explicitly makes `discover -s tests` and `-m unittest
# tests.<name>` resolve the harness the same way.
sys.path.insert(0, str(HERE))
import babysit_fixture as cases  # noqa: E402
from bot_thread_fixture import BotThreadCases  # noqa: E402
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    REQUEST_CHANGES,
    Commit,
    Idle,
    Reply,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    MergeModeFixture,
)

import holophyte.agents  # noqa: E402 - after the sys.path insert above
import holophyte.operator  # noqa: E402 - after the sys.path insert above
import holophyte.pr  # noqa: E402 - after the sys.path insert above
import holophyte.pr_status  # noqa: E402 - after the sys.path insert above
from tests.test_cli_babysit import BabysitCliFixture  # noqa: E402


class CliMaintainerThreadTests(BabysitCliFixture, unittest.TestCase):
    def test_cli_note_becomes_a_pending_maintainer_instruction_after_resume(self):
        self.cli("--note", "fix the padding")
        state = self.pending()
        self.assertEqual(len(state.threads), 1)
        thread, = state.threads
        self.assertEqual(thread.author_kind, "maintainer")
        self.assertEqual(thread.body, "fix the padding")
        self.assertEqual(thread.author, "operator")
        self.assertTrue(thread.id.startswith("operator_note:"))


class MergeModeBabysitThreadsTests(cases.OperatorNoteCase, BotThreadCases,
                                 cases.BabysitHelpers, MergeModeFixture):
    """Thread judgment, bot policy, operator notes, and fix rounds."""
    def test_human_conversation_mention_is_fixed_and_replied_on_the_pull(self):
        self.human_conversation_mention_is_fixed_and_replied_on_the_pull()

    def test_bot_conversation_mentions_and_unmentioned_humans_are_ignored(self):
        self.bot_conversation_mentions_and_unmentioned_humans_are_ignored()

    def test_latest_mention_is_fixed_without_judgment_and_resolved(self):
        self.mentioned_thread_is_fixed(("reviewer", "User"))

    def test_advisory_bot_with_human_mention_is_an_instruction(self):
        self.mentioned_thread_is_fixed(("review-bot", "Bot"))

    def mentioned_thread_is_fixed(self, opener):
        self.configure('[merge]\nmode = "pr"\nbot_threads = "advisory"\n')
        thread = ("src/app.py", 30, opener, "Which token?",
                  ((("operator", "User"),
                    "@HoLoPhYtE drop guestTokenId and use the path tokenId"),))
        self.fake_route(states=[self.pr_state([thread]), self.pr_state()])
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: use path token"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertNotIn("adjudicate", fake.roles)
        self.assertEqual(fake.roles[:4],
                         ["implement", "review", "implement", "implement"])
        goal = fake.turns[3].goal
        self.assertIn("Instruction from @operator on the pull request:", goal)
        self.assertIn("drop guestTokenId and use the path tokenId", goal)
        replies = [data for kind, data in self.api_calls() if kind == "reply"]
        self.assertEqual(len(replies), 1)
        self.assertIn("Addressed in ", replies[0]["body"])
        self.assertIn(("resolve", {"thread": "PRRT_1"}), self.api_calls())

    def test_two_instructions_are_stored_with_posted_outcomes(self):
        self.configure('[merge]\nmode = "pr"\n')
        threads = [("src/app.py", 30, ("operator", "User"),
                    "@holophyte use the path token\n  and reject missing tokens"),
                   ("src/app.py", 40, ("maintainer", "User"),
                    "@holophyte preserve validation")]
        self.fake_route(states=[self.pr_state(threads), self.pr_state()])
        pending = []
        original = holophyte.pr.reply_thread

        def reply(*args, **kwargs):
            pending.extend(json.loads(self.read(
                "SELECT findings FROM reviewRounds WHERE round = 2")[0][0]))
            return original(*args, **kwargs)

        with patch("holophyte.pr.reply_thread", side_effect=reply):
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
                    "@holophyte fix tokens") for author in authors]
        threads.append(("src/app.py", 40, ("service", "User"), "Which token?",
                        ((("operator", "User"), "@holophyte preserve validation"),)))
        self.fake_route(states=[self.pr_state(threads), self.pr_state()])
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  Commit("fix tokens"), APPROVE, Idle(""), provider=self.provider())
        findings = json.loads(self.read(
            "SELECT findings FROM reviewRounds WHERE round = 2")[0][0])
        self.assertEqual(len(findings), 4)
        for finding, author in zip(findings, authors):
            self.assertEqual(finding["kind"], "finding")
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
             ((("operator", "User"), "@holophyte preserve validation"),)),
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
            ("src/app.py", 40, ("reviewer", "User"), "@holophyte rename it",
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
        self.assertIn("@reviewer: @holophyte rename it", goal)
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
                    "@factory-bot use path tokenId"),
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
        with patch("holophyte.babysitter._verdicts_by_kind",
                   return_value={1: ("DECLINE", "a naming preference")}):
            _, calls = self.declined_thread(("alice", "User"))
        self.assertEqual([kind for kind, _ in calls], ["state", "reply"])
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertIn("1 thread(s) declined and left open", self.question())
        self.assertIn("@alice", self.question())

    def test_a_stalled_fix_round_keeps_implementer_output(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT])])
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- a real crash"),
                            Idle("reading store/read.py\nstill reading"),
                            provider=self.provider())

        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("failed", "failed")])
        self.assertEqual(self.git("rev-parse", BRANCH).strip(),
                         fake.turns[1].candidate_sha)
        ((summary, payload),) = self.read(
            "SELECT summary, payload FROM runEvents"
            " WHERE kind = 'implementer_output'")
        self.assertEqual(summary, "fix round 1: reading store/read.py")
        self.assertIn("reading store/read.py\nstill reading", payload)

    def test_fix_transport_retry_preserves_the_pr_on_second_failure(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT])])
        failed = Idle(holophyte.agents.ImplementerOutput("fetch failed", 1))
        with patch("holophyte.loop.sleep") as nap:
            fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                                Reply("THREAD 1: ADDRESS -- a real crash"),
                                failed, failed, provider=self.provider())
        nap.assert_called_once_with(30)
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(self.git("rev-parse", BRANCH).strip(),
                         fake.turns[1].candidate_sha)
        self.assertEqual(self.read("SELECT count(*) FROM runEvents"
                                   " WHERE kind = 'transport_retry'"), [(1,)])
        self.assertEqual(self.read("SELECT payload FROM runEvents"
                                   " WHERE kind = 'implementer_output'"),
                         [("fetch failed",), ("fetch failed",)])

    def test_a_fix_round_that_moves_the_candidate_keeps_no_output_event(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]),
                                self.pr_state()])
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- a real crash"),
                            Commit("fix: default load()"), APPROVE, Idle(""),
                            provider=self.provider())

        self.assertNotEqual(fake.turns[1].candidate_sha,
                            fake.turns[5].candidate_sha)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(self.read("SELECT count(*) FROM runEvents"
                                   " WHERE kind = 'implementer_output'"),
                         [(0,)])

    def test_a_fix_round_is_reviewed_before_the_pr_is_auto_merged(self):
        """Review the fixed candidate independently before merging it."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]),
                                self.pr_state()])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- a real crash"),
                            Commit("fix: default load()"), APPROVE, Idle(""),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement", "adjudicate",
                                      "implement", "review", "implement"])
        self.assertIn("ADDRESS: a real crash", fake.turns[6].goal)
        merge = [v for kind, v in self.api_calls() if kind == "merge"]
        self.assertEqual(len(merge), 1)
        fixed = merge[0]["sha"]
        self.assertNotEqual(fixed, fake.turns[1].candidate_sha)
        self.assertEqual(fake.turns[5].candidate_sha, fixed)
        self.assertEqual(fake.turns[5].base_sha, self.base)
        self.assertIn(fake.turns[1].candidate_sha[:12], fake.turns[5].goal)
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "reply", "resolve", "state", "merge"])
        self.assertEqual(
            self.read("SELECT round, verdict, reviewerModel FROM reviewRounds"
                      " ORDER BY round"),
            [(1, "pass", holophyte.agents.agent_route(self.tgt, "review")),
             (2, "changes_requested", "github:review-bot"),
             (3, "pass", "github:ci"),
             (4, "pass", holophyte.agents.agent_route(self.tgt, "review"))])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])

    def test_human_fix_refreshes_description_before_parking(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]), self.pr_state()])
        fake, _ = self.loop(
            Commit("the scripted work"), APPROVE, Idle(""),
            Reply("THREAD 1: ADDRESS -- a real crash"),
            Commit("fix: default load()"),
            Idle("TITLE: Fixed load\nLoad handles missing input."),
            provider=self.provider())

        self.assertEqual(fake.roles.count("review"), 1)
        self.assertEqual([c for c in self.recorded() if c.startswith("gh pr edit")],
                         [f"gh pr edit {self.URL} --body-file -"])
        history = self.pr_body.read_text().split("## Changes since first review\n")[1]
        self.assertTrue(history.startswith("- Round 1:"))
        self.assertIn("ADDRESS: a real crash", history)
        fixed = self.git("rev-parse", BRANCH).strip()
        original = fake.turns[1].candidate_sha[:12]
        self.assertIn(
            f"the fix rounds moved the candidate from {original} to {fixed[:12]};"
            f" the release covered {original}, and a human says merge on the"
            ' candidate as it stands ([merge] approve = "human")', self.question())
        self.assertEqual(self.read("SELECT phase, candidateSha FROM runs"),
                         [("awaiting_merge_approval", fixed)])

    def test_fix_verify_failure_redacts_environment_from_print_and_outcome(self):
        source = self.target.parent / "source.env"
        source.write_bytes(b"PUBLIC=sentinel-fix-value\r\n")
        self.configure('[merge]\nmode = "pr"\n'
                       f'[worktree]\nenv_source = "{source}"\n'
                       'env_allow = ["PUBLIC"]\n'
                       '[verify]\nalways = '
                       '["test ! -f break-verify || { cat .env; exit 1; }"]\n')
        self.fake_route(states=[self.pr_state([self.DEFECT])])
        output = self.main_output(
            Commit("candidate"), APPROVE, Idle(""),
            Reply("THREAD 1: ADDRESS -- a real crash"),
            Commit("fix", path="break-verify"), provider=self.provider())
        self.assertIn("verify FAILED after the fix round", output)
        self.assertIn("PUBLIC=[redacted]", output)
        self.assertNotIn("sentinel-fix-value", output)
        reason, = self.read("SELECT outcomeReason FROM runs")
        self.assertNotIn("sentinel-fix-value", reason[0])
        self.assertIn("[redacted]", reason[0])

    def test_human_fix_failed_verify_parks_without_description_edit(self):
        failure = self.db.parent / "verify-failed"
        command = f"test ! -f {failure}"
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       f'[verify]\nalways = ["{command}"]\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]), self.pr_state()])
        push = holophyte.pr.push_branch
        pushes = []

        def push_then_break_verify(*args, **kwargs):
            result = push(*args, **kwargs)
            pushes.append(result)
            if len(pushes) == 2:
                failure.touch()
            return result

        with patch.object(holophyte.pr, "push_branch", push_then_break_verify):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      Reply("THREAD 1: ADDRESS -- a real crash"),
                      Commit("fix: default load()"), provider=self.provider())

        self.assertIn(command, self.question())
        self.assertIn("verify failed", self.question())
        self.assertFalse(any(c.startswith("gh pr edit") for c in self.recorded()))
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.read("SELECT phase, candidateSha FROM runs"),
                         [("awaiting_merge_approval", fixed)])

    def test_a_fix_round_the_reviewer_rejects_parks_instead_of_merging(self):
        """An initial PR pass rejection parks; only a resume gets the allowance."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]),
                                self.pr_state()])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- a real crash"),
                            Commit("fix: default load()"), REQUEST_CHANGES,
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement", "adjudicate",
                                      "implement", "review"])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "reply", "resolve", "state"])
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(
            self.read("SELECT phase, outcome, candidateSha FROM runs"),
            [("awaiting_merge_approval", None, fixed)])
        self.assertEqual(
            self.read("SELECT verdict FROM reviewRounds WHERE round = 4"),
            [("changes_requested",)])
        question = self.question()
        self.assertIn(fixed[:12], question)
        self.assertIn("scripted change is incomplete", question)

    def test_babysit_review_dispatches_one_fix_with_findings_and_note(self):
        self.resume_rejected_fix()
        self.configure('[merge]\nmode = "pr"\n'
                       '[verify]\nalways = ["git rev-parse HEAD"]\n')
        fake, _ = self.loop(REQUEST_CHANGES, Commit("review fix"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertEqual(fake.roles, ["review", "implement", "review", "implement"])
        self.assertIn("scripted change is incomplete", fake.turns[1].goal)
        self.assertIn("repair the pin", fake.turns[1].goal)
        self.assertIn("repair the pin", fake.turns[3].goal)
        fixed = fake.turns[2].candidate_sha
        self.assertNotEqual(fixed, fake.turns[0].candidate_sha)
        self.assertEqual([v["sha"] for kind, v in self.api_calls()
                          if kind == "merge"], [fixed])
        self.assertEqual(self.read("SELECT verdict FROM reviewRounds"
                                   " WHERE runId = 2 AND reviewerModel NOT LIKE"
                                   " 'github:%' ORDER BY round"),
                         [("changes_requested",), ("pass",)])
        results = json.loads(self.read("SELECT verificationResults FROM"
                                       " reviewRounds WHERE runId = 2"
                                       " AND verdict = 'changes_requested'"
                                       " AND reviewerModel NOT LIKE 'github:%'")[0][0])
        self.assertEqual([r["exitCode"] for r in results], [0, 0])
        baseline = [r for r in results if r["source"] == "baseline"]
        self.assertEqual(baseline[0]["output"].strip(), fake.turns[0].candidate_sha)

    def test_allowance_refresh_keeps_original_address_and_retry_context(self):
        self.resume_rejected_fix()
        self.serve(self.pr_state([self.DEFECT]), self.pr_state())
        fake, _ = self.loop(
            Reply("THREAD 1: ADDRESS -- preserve missing input"),
            Commit("thread fix on resume"), REQUEST_CHANGES,
            Commit("review fix"), APPROVE,
            Idle("TITLE: Updated description\nBoth defects fixed."),
            provider=self.provider())

        prompt = fake.turns[-1].goal
        body = self.pr_body.read_text()
        for text in ("ADDRESS: preserve missing input",
                     "scripted change is incomplete", "repair the pin"):
            self.assertIn(text, prompt)
            self.assertIn(text, body)
        self.assertIn("## Changes since first review\n- Round 1:", body)

    def test_babysit_review_fix_waits_for_head_and_checks_before_merging(self):
        old, fake, naps = self.review_fix_propagation(catches_up=True)
        self.assertEqual(fake.roles, ["review", "implement", "review", "implement"])
        fixed = fake.turns[2].candidate_sha
        self.assertNotEqual(old, fixed)
        self.assertEqual([v["sha"] for kind, v in self.api_calls() if kind == "merge"],
                         [fixed])
        self.assertEqual(naps, [5, holophyte.pr.CHECK_POLL_S])
        self.assertEqual(self.read("SELECT outcome FROM runs WHERE id = 2"),
                         [("merged",)])

    def test_babysit_review_fix_stale_api_uses_remote_head(self):
        old, fake, naps = self.review_fix_propagation(catches_up=False)
        self.assertEqual(fake.roles, ["review", "implement", "review", "implement"])
        fixed = fake.turns[2].candidate_sha
        self.assertNotEqual(old, fixed)
        self.assertEqual([v["sha"] for kind, v in self.api_calls() if kind == "merge"],
                         [fixed])
        self.assertEqual(sum(naps), 15)
        self.assertEqual(self.read("SELECT outcome FROM runs WHERE id = 2"),
                         [("merged",)])

    def test_babysit_gets_a_recorded_fix_round_past_the_spent_cap(self):
        self.resume_rejected_fix()
        review = cases.SpentCapReview(self.db, REQUEST_CHANGES)
        fake, _ = self.loop(review, Commit("fix past cap"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertEqual(fake.roles, ["review", "implement", "review", "implement"])
        self.assertEqual(review.count, 1)
        self.assertEqual(self.read("SELECT reviewRoundCap, reviewRoundCount"
                                   " FROM runs WHERE id = 2"), [(1, 4)])
        self.assertEqual(self.read("SELECT verdict FROM reviewRounds"
                                   " WHERE runId = 2 AND round = 3"), [("pass",)])

    def test_babysit_second_rejection_parks_without_another_fix(self):
        self.resume_rejected_fix()
        fake, _ = self.loop(REQUEST_CHANGES, Commit("review fix"), REQUEST_CHANGES,
                            provider=self.provider())
        self.assertEqual(fake.roles, ["review", "implement", "review"])
        self.assertFalse([v for kind, v in self.api_calls() if kind == "merge"])
        self.assertIn("the review of the fix at "
                      + fake.turns[2].candidate_sha[:12] + " asked for changes",
                      self.question())


    def test_the_review_of_a_fix_is_held_to_the_criteria(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]),
                                self.pr_state()])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- a real crash"),
                            Commit("fix: default load()"),
                            Reply("CRITERION 1: unwitnessed \u2014 no test"
                                  " covers the fix\nVERDICT: APPROVE"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement", "adjudicate",
                                      "implement", "review"])
        self.assertIn("Acceptance criteria, numbered:", fake.turns[5].goal)
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "reply", "resolve", "state"])
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(
            self.read("SELECT phase, outcome, candidateSha, approvedSha,"
                      " mergeSha FROM runs"),
            [("awaiting_merge_approval", None, fixed, None, None)])
        self.assertEqual(
            self.read("SELECT verdict FROM reviewRounds WHERE round = 4"),
            [("changes_requested",)])
        self.assertIn("CRITERION 1: unwitnessed", self.question())


    def test_a_pass_fixes_the_defect_declines_the_nit_and_parks(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT, self.NIT])])
        provider = self.provider()
        verdicts = Reply("THREAD 1: ADDRESS -- load() must not return None"
                         " on a missing file\n"
                         "THREAD 2: DECLINE -- a naming preference, not a"
                         " defect")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""), verdicts,
                            Commit("fix: default load() to an empty thing"),
                            provider=provider)

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "adjudicate", "implement"])
        # The adjudicator judged the candidate as pushed, against main.
        self.assertEqual(fake.turns[3].base_sha, self.base)
        self.assertIn(self.URL, fake.turns[3].goal)
        self.assertIn(self.DEFECT[3], fake.turns[3].goal)
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertNotEqual(fixed, fake.turns[3].candidate_sha)
        self.assertIn("fix: default load() to an empty thing",
                      self.subjects(BRANCH))
        # Two pushes: the candidate, then the fix.
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"] * 2)
        calls = self.api_calls()
        self.assertEqual([kind for kind, _ in calls],
                         ["state", "reply", "resolve", "reply"])
        model = holophyte.agents.agent_route(self.tgt, "adjudicate")
        self.assertEqual(calls[1][1]["thread"], "PRRT_1")
        self.assertTrue(calls[1][1]["body"].startswith(
            f"---- Comment by {model} ----\n"), calls[1][1]["body"])
        self.assertIn(fixed, calls[1][1]["body"])
        self.assertEqual(calls[2][1], {"thread": "PRRT_1"})
        self.assertEqual(calls[3][1]["thread"], "PRRT_2")
        self.assertIn("Declined:", calls[3][1]["body"])
        self.assertIn("naming preference", calls[3][1]["body"])
        self.assertEqual(
            self.read("SELECT round, verdict, reviewerModel FROM reviewRounds"
                      " ORDER BY round"),
            [(1, "pass", holophyte.agents.agent_route(self.tgt, "review")),
             (2, "changes_requested", "github:review-bot+style-bot")])
        # Every reply and resolve is on the run's stream.
        events = [summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'pull_request'"
            " ORDER BY seq")]
        self.assertEqual(
            [e.split(" thread ")[0] for e in events if " thread " in e],
            ["replied on", "resolved", "replied on"])
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl, candidateSha FROM runs"),
            [("awaiting_merge_approval", None, self.URL, fixed)])
        question = self.question()
        self.assertTrue(question.startswith(f"PR open: {self.URL}\n"))
        self.assertIn("1 thread(s) declined", question)
        self.assertIn(self.NIT[3], question)
        self.assertNotIn(self.DEFECT[3], question)


    def test_a_fix_round_that_leaves_edits_is_not_pushed_or_resolved(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT])])
        wt = self.worktrees / "ko-131-add-a-thing"

        class CommitLeavingEdits(Commit):
            def play(self, cwd, turn):
                out = super().play(cwd, turn)
                (cwd / "rest-of-the-fix.py").write_text("not committed\n")
                return out

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- a real crash"),
                            CommitLeavingEdits("fix: half of it"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "adjudicate", "implement"])
        # The candidate's push only; the fix never left the machine.
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"])
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        self.assertIn("fix: half of it", self.subjects(BRANCH))
        self.assertTrue((wt / "rest-of-the-fix.py").exists())
        self.assertNotIn("WIP", self.subjects(BRANCH))
        ((outcome, reason),) = self.read(
            "SELECT outcome, outcomeReason FROM runs")
        self.assertEqual(outcome, "failed")
        self.assertIn("uncommitted", reason)


    def test_a_thread_follow_up_reaches_the_adjudicator_and_the_question(self):
        self.configure('[merge]\nmode = "pr"\n')
        follow_up = ("Hold on: do we want load() to default at all? Asking"
                     " before anything is changed here.")
        thread = self.DEFECT + ([("ko", follow_up)],)
        self.fake_route(states=[self.pr_state([thread])])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: HUMAN -- the operator asked"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement", "adjudicate"])
        goal = fake.turns[3].goal
        self.assertIn(self.DEFECT[3], goal)
        self.assertIn(follow_up, goal)
        self.assertLess(goal.index(self.DEFECT[3]), goal.index(follow_up))
        self.assertIn("@ko", goal)
        question = self.question()
        self.assertIn(f"> {self.DEFECT[3]}", question)
        self.assertIn(f"> {follow_up}", question)
        self.assertIn("@ko", question)


    def test_a_thread_with_a_second_page_of_comments_is_read_to_the_end(self):
        self.configure('[merge]\nmode = "pr"\n')
        first_reply = ("the-bot", "Still applies after the rebase.")
        last_word = "Please leave this exactly as it is; I will explain in" \
                    " the ticket."
        self.fake_route(
            states=[self.pr_state([self.NIT])],
            comments=[self.comments_page(1, [("ko", last_word)])])
        state = json.loads((Path(self.calls).parent / "states"
                            / "001.json").read_text())
        thread = state["data"]["repository"]["pullRequest"][
            "reviewThreads"]["nodes"][0]
        thread["comments"]["nodes"].append(
            self.comment("1_1", *first_reply))
        thread["comments"]["pageInfo"] = {"hasNextPage": True,
                                          "endCursor": "k1"}
        (Path(self.calls).parent / "states" / "001.json").write_text(
            json.dumps(state))

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: HUMAN -- the operator said so"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement", "adjudicate"])
        calls = self.api_calls()
        self.assertEqual([kind for kind, _ in calls],
                         ["state", "comments"])
        self.assertEqual(calls[1][1]["thread"], "PRRT_1")
        self.assertEqual(calls[1][1]["after"], "k1")
        goal = fake.turns[3].goal
        self.assertIn(first_reply[1], goal)
        self.assertIn(last_word, goal)
        self.assertLess(goal.index(first_reply[1]), goal.index(last_word))


    def test_a_human_verdict_posts_nothing_and_parks_with_the_thread(self):
        self.configure('[merge]\nmode = "pr"\n')
        asks = ("src/app.py", 30, "ko",
                "Do we want this to be configurable at all?")
        self.fake_route(states=[self.pr_state([asks])])
        verdict = Reply("THREAD 1: HUMAN -- a question about the approach"
                        " for the operator")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""), verdict,
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement", "adjudicate"])
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"])
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl FROM runs"),
            [("awaiting_merge_approval", None, self.URL)])
        question = self.question()
        self.assertIn("needs a human's answer", question)
        self.assertIn(f"> {asks[3]}", question)
        self.assertIn("src/app.py:30 by @ko", question)
        self.assertEqual(
            self.read("SELECT verdict, reviewerModel FROM reviewRounds"
                      " WHERE round = 2"),
            [("changes_requested", "github:ko")])


    def test_a_thread_a_person_opened_is_human_before_the_adjudicator(self):
        self.configure('[merge]\nmode = "pr"\n')
        person = ("src/app.py", 30, ("wevial", "User"),
                  "I would rather this stayed as it was; leaving my reasons"
                  " on the ticket.")
        self.fake_route(states=[self.pr_state([person, self.DEFECT])])
        verdicts = Reply("THREAD 1: ADDRESS -- a real crash\n"
                         "THREAD 2: ADDRESS -- whatever it is, fix it")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""), verdicts,
                            Commit("fix: never reached"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement", "adjudicate"])
        goal = fake.turns[3].goal
        self.assertIn(self.DEFECT[3], goal)
        self.assertIn("THREAD 1 -- src/app.py:10 by @review-bot", goal)
        self.assertNotIn(person[3], goal)
        self.assertNotIn("wevial", goal)
        self.assertNotIn("THREAD 2", goal)
        # Nothing posted: no reply, no resolve, no fix pushed.
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"])
        ((findings, route),) = self.read(
            "SELECT findings, reviewerModel FROM reviewRounds"
            " WHERE round = 2")
        messages = [f["message"] for f in json.loads(findings)]
        self.assertEqual(len(messages), 2, messages)
        self.assertIn("src/app.py:30 @wevial", messages[0])
        self.assertIn("-- HUMAN: opened by a person", messages[0])
        self.assertIn("src/app.py:10 @review-bot", messages[1])
        self.assertIn("-- ADDRESS: a real crash", messages[1])
        self.assertEqual(route, "github:review-bot+wevial")
        ((ledger,),) = self.read(
            "SELECT text FROM ledger WHERE kind = 'round' AND text LIKE"
            " 'Babysit pass%'")
        self.assertIn("1 opened by a person, HUMAN before the adjudicator",
                      ledger)
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl FROM runs"),
            [("awaiting_merge_approval", None, self.URL)])
        question = self.question()
        self.assertIn("needs a human's answer", question)
        self.assertIn(f"> {person[3]}", question)
        self.assertIn("src/app.py:30 by @wevial", question)


    def test_under_act_a_person_s_address_is_fixed_replied_and_left_open(self):
        self.configure('[merge]\nmode = "pr"\nhuman_threads = "act"\n')
        person = ("src/app.py", 30, ("wevial", "User"),
                  "Rename `thing` to `default_thing` here; the bare name"
                  " shadows the module.")
        self.fake_route(states=[self.pr_state([person])])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- rename as asked"),
                            Commit("fix: rename thing to default_thing"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "adjudicate", "implement"])
        goal = fake.turns[3].goal
        self.assertIn("THREAD 1 -- src/app.py:30 by @wevial", goal)
        self.assertIn(person[3], goal)
        self.assertIn("opened by a person", goal)
        self.assertIn("Never DECLINE a person's thread", goal)
        # The fix round was given the person's thread.
        self.assertIn(person[3], fake.turns[4].goal)
        self.assertIn("@wevial", fake.turns[4].goal)
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"] * 2)
        calls = self.api_calls()
        self.assertEqual([kind for kind, _ in calls], ["state", "reply"])
        model = holophyte.agents.agent_route(self.tgt, "adjudicate")
        self.assertEqual(calls[1][1]["thread"], "PRRT_1")
        self.assertTrue(calls[1][1]["body"].startswith(
            f"---- Comment by {model} ----\n"), calls[1][1]["body"])
        self.assertIn(fixed, calls[1][1]["body"])
        events = [summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'pull_request'"
            " ORDER BY seq")]
        self.assertEqual(
            [e.split(" thread ")[0] for e in events if " thread " in e],
            ["replied on"])
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl, candidateSha FROM runs"),
            [("awaiting_merge_approval", None, self.URL, fixed)])
        question = self.question()
        self.assertNotIn("needs a human's answer", question)
        self.assertIn("1 person's thread(s) addressed and left open",
                      question)
        self.assertIn("src/app.py:30 (@wevial)", question)


    def test_under_act_a_declined_person_is_human_and_the_bot_is_fixed(self):
        self.configure('[merge]\nmode = "pr"\nhuman_threads = "act"\n')
        person = ("src/app.py", 30, ("wevial", "User"),
                  "Should this be configurable at all? I would leave it.")
        self.fake_route(states=[self.pr_state([person, self.DEFECT])])
        verdicts = Reply("THREAD 1: DECLINE -- a preference, not a defect\n"
                         "THREAD 2: ADDRESS -- a real crash")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""), verdicts,
                            Commit("fix: default load() to an empty thing"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "adjudicate", "implement"])
        self.assertIn("THREAD 1 -- src/app.py:30 by @wevial",
                      fake.turns[3].goal)
        self.assertIn("THREAD 2 -- src/app.py:10 by @review-bot",
                      fake.turns[3].goal)
        self.assertNotIn(person[3], fake.turns[4].goal)
        self.assertIn(self.DEFECT[3], fake.turns[4].goal)
        fixed = self.git("rev-parse", BRANCH).strip()
        calls = self.api_calls()
        self.assertEqual([kind for kind, _ in calls],
                         ["state", "reply", "resolve"])
        self.assertEqual(calls[1][1]["thread"], "PRRT_2")
        self.assertIn(fixed, calls[1][1]["body"])
        self.assertEqual(calls[2][1], {"thread": "PRRT_2"})
        ((findings,),) = self.read(
            "SELECT findings FROM reviewRounds WHERE round = 2")
        messages = [f["message"] for f in json.loads(findings)]
        self.assertIn("-- HUMAN: a person's thread the adjudicator would not"
                      " address", messages[0])
        self.assertIn("-- ADDRESS: a real crash", messages[1])
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl, candidateSha FROM runs"),
            [("awaiting_merge_approval", None, self.URL, fixed)])
        question = self.question()
        self.assertIn("needs a human's answer", question)
        self.assertIn(f"> {person[3]}", question)
        self.assertIn("src/app.py:30 by @wevial", question)
        self.assertNotIn(self.DEFECT[3], question)


    def test_under_act_a_bot_s_human_verdict_still_parks_before_acting(self):
        self.configure('[merge]\nmode = "pr"\nhuman_threads = "act"\n')
        asks = ("src/app.py", 30, "ask-bot",
                "Is this API shape what the operator wants long term?")
        self.fake_route(states=[self.pr_state([asks, self.DEFECT])])
        verdicts = Reply("THREAD 1: HUMAN -- a design question for the"
                         " operator\nTHREAD 2: ADDRESS -- a real crash")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""), verdicts,
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement", "adjudicate"])
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"])
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl FROM runs"),
            [("awaiting_merge_approval", None, self.URL)])
        question = self.question()
        self.assertIn("needs a human's answer", question)
        self.assertIn(f"> {asks[3]}", question)
        self.assertNotIn(f"> {self.DEFECT[3]}", question)


if __name__ == "__main__":
    unittest.main()
