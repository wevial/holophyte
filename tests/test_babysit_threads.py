"""`holophyte.babysitter`'s pass under `[merge] mode = "pr"`, end to end."""
from __future__ import annotations

import io
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
    REQUEST_CHANGES,
    Commit,
    Idle,
    Reply,
)
from loop_fixture import BRANCH, LoopFixture, MergeModeFixture  # noqa: E402
from mention_accounts_fixture import MentionAccountCases  # noqa: E402
from triage_mention_fixture import TriageMentionCases  # noqa: E402

import holophyte.agents  # noqa: E402 - after the sys.path insert above
import holophyte.gates  # noqa: E402 - after the sys.path insert above
import holophyte.operator  # noqa: E402 - after the sys.path insert above
import holophyte.pr  # noqa: E402 - after the sys.path insert above
import holophyte.pr_status  # noqa: E402 - after the sys.path insert above
from holophyte.maintainer_notes import cite_commits  # noqa: E402
from holophyte.thread_mentions import REFUSAL  # noqa: E402


@dataclass
class SeesCalls(Commit):
    """A fix turn that notes the GitHub calls already made when it began."""

    fixture: object = None
    seen: list = field(default_factory=list)

    def play(self, cwd, turn):
        self.seen.extend(kind for kind, _ in self.fixture.api_calls())
        return super().play(cwd, turn)


class MergeModeBabysitThreadsTests(MentionAccountCases, TriageMentionCases,
                                 AskMentionCases,
                                 cases.OperatorNoteCase,
                                 BotThreadCases, cases.BabysitHelpers,
                                 MergeModeFixture):
    """Thread judgment, bot policy, operator notes, and fix rounds."""
    def test_answered_no_commit_threads_park_and_accept_corrected_instruction(self):
        self.no_commit_thread_answers("complete")

    def test_dirty_no_commit_threads_fail(self):
        self.no_commit_thread_answers("dirty")

    def test_no_commit_park_redacts_configured_secrets(self):
        self.no_commit_thread_answers("secret")

    def test_no_commit_review_fix_without_threads_still_fails(self):
        self.no_commit_review_fix_fails()

    def test_partially_answered_no_commit_threads_fail(self):
        self.no_commit_thread_answers("partial")

    def test_budget_cutoff_with_all_thread_answers_fails(self):
        self.no_commit_thread_answers("timeout")

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
        raw = "<details>analysis chain</details>\n**Forced file**"
        self.fake_route(states=[self.pr_state([
            ("src/app.py", 10, ("review-bot", "Bot"), raw)])])
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- the index keeps a forced file"),
                            Idle("reading store/read.py\nstill reading"),
                            provider=self.provider())

        finding, = json.loads(self.read(
            "SELECT findings FROM reviewRounds WHERE round = 2")[0][0])
        self.assertEqual(finding, dict(
            kind="thread", author="review-bot", author_kind="bot", verdict="ADDRESS",
            summary="the index keeps a forced file",
            message="the index keeps a forced file",
            path="src/app.py", line=10, url=self.URL + "#discussion_r1",
            severity="p2", raw=raw))
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("failed", "failed")])
        self.assertEqual(self.git("rev-parse", BRANCH).strip(),
                         fake.turns[1].candidate_sha)
        ((summary, payload),) = self.read(
            "SELECT summary, payload FROM runEvents"
            " WHERE kind = 'implementer_output'")
        self.assertEqual(summary, "still reading")
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
            [(1, "pass", holophyte.agents.agent_route(self.project, "review")),
             (2, "changes_requested", "github:review-bot"),
             (3, "pass", "github:ci"),
             (4, "pass", holophyte.agents.agent_route(self.project, "review"))])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])

    def test_human_fix_refreshes_description_before_parking(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]), self.pr_state()])
        fake, _ = self.loop(
            Commit("the scripted work"), APPROVE, Idle(""),
            Reply("THREAD 1: ADDRESS -- a real crash"),
            Commit("fix: default load()"),
            Idle("TITLE: Fixed load\nLoad handles missing input.\n\n"
                 "## Changes since first review\n- Missing input no longer crashes."),
            provider=self.provider())

        self.assertEqual(fake.roles.count("review"), 1)
        self.assertEqual([c for c in self.recorded() if c.startswith("gh pr edit")],
                         [f"gh pr edit {self.URL} --body-file -"])
        body = self.pr_body.read_text()
        self.assertIn("Load handles missing input.", body)
        self.assertNotIn("Changes since first review", body)
        fixed = self.git("rev-parse", BRANCH).strip()
        original = fake.turns[1].candidate_sha[:12]
        self.assertIn(
            f"the fix rounds moved the candidate from {original} to {fixed[:12]};"
            f" the release covered {original}, and a human says merge on the"
            ' candidate as it stands ([merge] approve = "human")', self.question())
        self.assertEqual(self.read("SELECT phase, candidateSha FROM runs"),
                         [("awaiting_merge_approval", fixed)])

    def test_human_review_fixes_reviews_the_fix_range_before_parking(self):
        """KO-663: `review_fixes` puts the fix range to a covering review,
        and the park names what that review covered."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       'review_fixes = true\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]), self.pr_state()])
        fake, _ = self.loop(
            Commit("the scripted work"), APPROVE, Idle(""),
            Reply("THREAD 1: ADDRESS -- a real crash"),
            Commit("fix: default load()"), APPROVE, Idle(""),
            provider=self.provider())

        self.assertEqual(fake.roles.count("review"), 2)
        released = fake.turns[1].candidate_sha
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertIn(f"{released}..{fixed}", fake.turns[5].goal)
        self.assertIn(f"ready to merge; fix commits since {released[:12]}"
                      f" reviewed at {fixed[:12]}; waiting for a human to say"
                      ' merge ([merge] approve = "human")', self.question())
        self.assertEqual(
            self.read("SELECT phase, candidateSha, approvedSha FROM runs"),
            [("awaiting_merge_approval", fixed, fixed)])
        self.assertFalse([v for kind, v in self.api_calls() if kind == "merge"])

    def test_human_review_fixes_rejection_parks_without_approval(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       'review_fixes = true\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]), self.pr_state()])
        fake, _ = self.loop(
            Commit("the scripted work"), APPROVE, Idle(""),
            Reply("THREAD 1: ADDRESS -- a real crash"),
            Commit("fix: default load()"), REQUEST_CHANGES,
            provider=self.provider())

        self.assertEqual(fake.roles.count("review"), 2)
        fixed = self.git("rev-parse", BRANCH).strip()
        question = self.question()
        self.assertIn(f"the review of the fix at {fixed[:12]} asked for"
                      " changes", question)
        self.assertIn("scripted change is incomplete", question)
        self.assertEqual(
            self.read("SELECT phase, candidateSha, approvedSha FROM runs"),
            [("awaiting_merge_approval", fixed, None)])
        self.assertFalse([v for kind, v in self.api_calls() if kind == "merge"])
        self.assertFalse([c for c in self.recorded() if "pr merge" in c])

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
                holophyte.gates._PASSES.clear()  # a re-exec (KO-646)
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

    def test_auto_fix_failed_verify_parks_and_accepts_a_send_back(self):
        """KO-666: under automatic approval a verify that fails before the
        covering review parks on the PR, so `--babysit --note` can answer."""
        failure = self.db.parent / "verify-failed"
        command = f"test ! -f {failure}"
        self.configure('[merge]\nmode = "pr"\n'
                       f'[verify]\nalways = ["{command}"]\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]), self.pr_state()])
        push = holophyte.pr.push_branch
        pushes = []

        def push_then_break_verify(*args, **kwargs):
            result = push(*args, **kwargs)
            pushes.append(result)
            if len(pushes) == 2:
                failure.touch()
                holophyte.gates._PASSES.clear()  # a re-exec (KO-646)
            return result

        with patch.object(holophyte.pr, "push_branch", push_then_break_verify):
            fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                                Reply("THREAD 1: ADDRESS -- a real crash"),
                                Commit("fix: default load()"),
                                provider=self.provider())

        question = self.question()
        self.assertIn("verify failed", question)
        self.assertIn(command, question)
        self.assertEqual(fake.roles.count("review"), 1)
        route = holophyte.agents.agent_route(self.project, "review")
        self.assertEqual(self.read("SELECT count(*) FROM reviewRounds WHERE"
                                   f" reviewerModel = '{route}'"), [(1,)])
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.read("SELECT phase, outcome, candidateSha FROM runs"),
                         [("awaiting_merge_approval", None, fixed)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("blocked_on_operator",)])
        self.assertFalse([v for kind, v in self.api_calls() if kind == "merge"])

        holophyte.operator.babysit_ticket(self.project, "KO-131", "repin the file size",
                                          out=io.StringIO())
        self.assertEqual(self.read("SELECT status FROM tickets"), [("ready",)])
        (payload,), = self.read("SELECT payload FROM runEvents"
                                " WHERE kind = 'operator_note'")
        self.assertEqual(json.loads(payload)["note"], "repin the file size")

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
            Idle("TITLE: Updated description\nBoth defects fixed.\n\n"
                 "## Changes since first review\n- Missing input is preserved."),
            provider=self.provider())

        prompt = fake.turns[-1].goal
        body = self.pr_body.read_text()
        for text in ("ADDRESS: preserve missing input",
                     "scripted change is incomplete", "repair the pin"):
            self.assertIn(text, prompt)
            self.assertNotIn(text, body)
        self.assertIn("Both defects fixed.", body)
        self.assertNotIn("Changes since first review", body)

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
        model = holophyte.agents.agent_route(self.project, "adjudicate")
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
            [(1, "pass", holophyte.agents.agent_route(self.project, "review")),
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
        self.assertIn(self.DEFECT[3], question)
        self.assertIn(follow_up, question)
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
        self.assertEqual(self.read("SELECT parkKind FROM runs"), [("thread",)])
        self.assertIn("needs a human's answer", question)
        self.assertIn(asks[3], question)
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
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"])
        ((findings, route),) = self.read(
            "SELECT findings, reviewerModel FROM reviewRounds"
            " WHERE round = 2")
        messages = [f["message"] for f in json.loads(findings)]
        self.assertEqual(len(messages), 2, messages)
        self.assertEqual(messages, ["opened by a person", "a real crash"])
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
        self.assertEqual(self.read("SELECT parkKind FROM runs"), [("thread",)])
        self.assertIn("needs a human's answer", question)
        self.assertIn(person[3], question)
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
        model = holophyte.agents.agent_route(self.project, "adjudicate")
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
        self.assertEqual(messages, ["a person's thread the adjudicator would not"
                                    " address", "a real crash"])
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl, candidateSha FROM runs"),
            [("awaiting_merge_approval", None, self.URL, fixed)])
        question = self.question()
        self.assertEqual(self.read("SELECT parkKind FROM runs"), [("thread",)])
        self.assertIn("needs a human's answer", question)
        self.assertIn(person[3], question)
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
        self.assertEqual(self.read("SELECT parkKind FROM runs"), [("thread",)])
        self.assertIn("needs a human's answer", question)
        self.assertIn(asks[3], question)
        self.assertNotIn("src/app.py:10 by @review-bot", question)


class OperatorNoteCitationTests(LoopFixture):
    """`cite_commits()` on real git: recording a citation never fails a fix."""
    ADDRESSED = [(7, holophyte.pr.Thread("operator_note:7", "", None, "maintainer",
                                         "change requested", "",
                                         author_kind="maintainer"), "")]

    def sh(self, argv, cwd):
        return self.git(*argv[1:], cwd=cwd).strip()

    def test_an_empty_last_commit_is_amended_to_carry_the_citation(self):
        self.git("commit", "-q", "--allow-empty", "-m",
                 "chore(merge): record that the merge already satisfies the note")
        fixed = self.git("rev-parse", "HEAD").strip()

        result = cite_commits(self.target, self.base, fixed, self.ADDRESSED, self.sh)

        self.assertNotEqual(result, fixed)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), result)
        self.assertTrue(self.git("log", "-1", "--format=%B").strip()
                        .endswith("operator_note event 7"))

    def test_a_citation_in_another_letter_case_is_not_amended(self):
        (self.target / "README.md").write_text("requested change\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "Fix per Operator_note event 7")
        fixed = self.git("rev-parse", "HEAD").strip()

        result = cite_commits(self.target, self.base, fixed, self.ADDRESSED, self.sh)
        self.assertEqual(result, fixed)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), fixed)


class ParkQuestionQuoteTests(unittest.TestCase):
    """`babysitter.quoted()`: a bot's thread reads as plain text in the park."""
    URL = "https://github.com/OWNER/NAME/pull/235#discussion_r9"
    # The shape of Greptile's P1 thread on pull request 235 (KO-714, run 624).
    GREPTILE = (
        '<a href="#"><img alt="P1" src="https://greptile-static-assets.s3'
        '.amazonaws.com/badges/p1.svg" align="top"></a> **Queue entry is'
        ' read before the merge settles**\n\n'
        "`merge_queue.py:116` reads the entry once; a dequeued pull request"
        " is then reported as merged.\n\n"
        "<details><summary>Prompt To Fix With AI</summary>\n\n"
        "`````markdown\nThis is a comment left during a code review.\n"
        "Path: holophyte/merge_queue.py\nLine: 116\n\n"
        "> Queue entry is read before the merge settles\n`````\n\n"
        "</details>\n\n")

    def quote(self, body):
        from holophyte import babysitter
        return babysitter.quoted(holophyte.pr.Thread(
            "1", "holophyte/merge_queue.py", 116, "greptile-apps[bot]", body,
            self.URL, author_kind="bot"))

    def test_a_greptile_comment_reads_as_its_badge_title_and_paragraph(self):
        text = self.quote(self.GREPTILE)
        self.assertIn("P1", text)
        self.assertIn("Queue entry is read before the merge settles", text)
        self.assertIn("reads the entry once; a dequeued pull request", text)
        self.assertNotRegex(text, r"<[a-zA-Z/][^>]*>")
        self.assertNotIn("details", text)
        self.assertNotIn("Prompt To Fix With AI", text)
        self.assertNotIn("This is a comment left during a code review", text)
        self.assertEqual([line for line in text.splitlines()
                          if line.lstrip().startswith(">")], [])

    def test_a_line_break_keeps_the_words_on_either_side_apart(self):
        text = self.quote("First<br>Second<br/>Third")
        self.assertEqual(text.split("\n", 1)[1].split(), ["First", "Second", "Third"])

    def test_block_tags_keep_their_texts_on_separate_lines(self):
        text = self.quote("<p>First</p><p>Second</p><ul><li>one</li><li>two</li></ul>")
        self.assertEqual([line for line in text.splitlines()[1:] if line.strip()],
                         ["First", "Second", "one", "two"])

    def test_a_long_comment_is_cut_to_600_characters_under_its_url(self):
        text = self.quote("x" * 900)
        header, body = text.split("\n", 1)
        self.assertIn(self.URL, header)
        self.assertEqual(len(body), 600)
        self.assertTrue(body.endswith("…"))


if __name__ == "__main__":
    unittest.main()
