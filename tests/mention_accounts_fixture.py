"""Mention identity boundaries exercised through the fake GitHub."""
import json
from pathlib import Path

from babysit_fixture import OperatorNotePass
from fake_agent import APPROVE, Commit, Idle, Reply
from loop_fixture import BRANCH

import holophyte.pr.pr_status
from holophyte.babysit import babysitter, thread_mentions
from holophyte.babysit.thread_mentions import REFUSAL
from holophyte.config.config_tables import merge_config

CODERABBIT = json.loads((Path(__file__).parent / "fixtures" / "mentions"
                         / "coderabbit_zip_member.json").read_text())
REFUSED = f"---- Comment by holophyte ----\n\n{REFUSAL}"


def coderabbit(replies=()):
    return (CODERABBIT["path"], CODERABBIT["line"],
            (CODERABBIT["login"], CODERABBIT["typename"]),
            CODERABBIT["body"], replies)


class MentionAccountCases(OperatorNotePass):
    def test_private_operator_note_is_exempt_from_mention_accounts(self):
        self.operator_note_pass(
            False, 'mention_accounts = ["github-maintainer"]\n',
            note="@holophyte fix: remove the subheader")

    def test_marked_mention_is_fixed_without_judgment_and_resolved(self):
        self.mentioned_thread_is_fixed(("reviewer", "User"))

    def test_advisory_bot_with_human_mention_is_an_instruction(self):
        self.mentioned_thread_is_fixed(("review-bot", "Bot"))

    def mentioned_thread_is_fixed(self, opener):
        self.configure('[merge]\nmode = "pr"\nbot_threads = "advisory"\n'
                       + getattr(self, "mention_accounts", ""))
        thread = ("src/app.py", 30, opener, "Which token?",
                  ((("operator", "User"),
                    "@HoLoPhYtE fix: drop guestTokenId and use the path tokenId"),))
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

    def test_review_bot_path_mention_is_adjudicated_not_refused(self):
        fake, replies = self.coderabbit_loop('mention_accounts = ["maintainer"]\n')
        fix = fake.roles.index("implement", fake.roles.index("adjudicate"))
        self.assertIn("Cap the uncompressed size of each ZIP member",
                      fake.turns[fix].goal)
        self.assertEqual(len(replies), 1)
        self.assertIn("Addressed in ", replies[0])

    def test_review_bot_path_mention_is_adjudicated_with_mentions_open(self):
        fake, _ = self.coderabbit_loop("")
        self.assertIn("adjudicate", fake.roles)

    def coderabbit_loop(self, accounts):
        """The loop over CodeRabbit's thread; asserts no instruction or refusal."""
        self.configure('[merge]\nmode = "pr"\n' + accounts)
        self.fake_route(states=[self.pr_state([coderabbit()]), self.pr_state()])
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- cap each member"),
                            Commit("fix: cap each member"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertFalse(any("Instruction from @coderabbitai" in turn.goal
                             for turn in fake.turns))
        replies = self.replies()
        self.assertFalse(any(REFUSAL in body for body in replies))
        return fake, replies

    def test_advisory_review_bot_path_mention_is_noted_not_refused(self):
        replies = self.settled('mention_accounts = ["maintainer"]\n'
                               'bot_threads = "advisory"\n', [coderabbit()])
        self.assertEqual(len(replies), 1)
        self.assertIn("Noted as advisory for the maintainer;"
                      " not acted on by the factory.", replies[0])
        self.assertNotIn(REFUSAL, replies[0])

    def test_stranger_mention_on_a_bot_thread_is_refused(self):
        thread = coderabbit(((("stranger", "User"), "@holophyte fix: cap it"),))
        replies = self.settled('mention_accounts = ["maintainer"]\n', [thread])
        self.assertEqual(replies, [REFUSED])

    def test_bot_mentions_are_never_instructions_nor_refused(self):
        threads = [("src/app.py", 30, author, "@holophyte fix: change tokens")
                   for author in (("robot", "Bot"), ("review-service[bot]", "User"),
                                  ("service", "User"))]
        replies = self.settled('mention_accounts = ["maintainer"]\n'
                               'bot_logins = ["service"]\n', threads)
        self.assertEqual(replies, [])
        self.assertEqual(self.mentioned(), [False] * 3)
        self.configure('[merge]\nmode = "pr"\nbot_logins = ["service"]\n')
        self.assertEqual(self.mentioned(), [False] * 3)

    def settled(self, merge, threads):
        """Replies posted by one settled babysit pass over `threads`."""
        self.configure('[merge]\nmode = "pr"\n' + merge)
        self.fake_route(states=[self.pr_state(threads)])
        self.git("branch", BRANCH)
        self.state = babysitter._settled_state(self.project, None, None, 1,
                                               self.pull())
        return self.replies()

    def mentioned(self):
        return [t.classification == "MENTIONED" for t in thread_mentions.classified(
                    self.state.threads, merge_config(self.project))]

    def pull(self):
        return holophyte.pr.pr_status.parse_pr_url(self.URL)

    def replies(self):
        return [data["body"] for kind, data in self.api_calls() if kind == "reply"]

    def test_listed_login_is_case_insensitive(self):
        self.mention_accounts = 'mention_accounts = ["OpErAtOr"]\n'
        self.mentioned_thread_is_fixed(("reviewer", "User"))

    def test_unlisted_mentions_keep_kind_and_refuse_once(self):
        request = "@holophyte fix: change tokens"
        thread = ("src/app.py", 30, ("stranger", "User"), request)
        replies = self.settled('mention_accounts = ["maintainer"]\n', [thread])
        merge = merge_config(self.project)
        classified = [thread_mentions.classify(t, merge.mention_handle,
                                              merge.mention_accounts)
                      for t in self.state.threads]
        self.assertEqual([t.author_kind for t in classified], ["user"])
        self.assertTrue(all(t.classification != "MENTIONED" for t in classified))
        self.assertEqual(replies, [REFUSED])
        self.serve(self.pr_state([(*thread, ((("factory", "User"), replies[0]),
                                             (("stranger", "User"), request)))]))
        babysitter._settled_state(self.project, None, None, 1, self.pull())
        self.assertEqual(self.replies(), [REFUSED])

    def test_unlisted_conversation_mention_refuses_once(self):
        self.configure('[merge]\nmode = "pr"\nmention_accounts = ["operator"]\n')
        state = self.conversation_state(("stranger", "User"),
                                        "@holophyte fix: change tokens")
        self.fake_route(states=[state])
        self.git("branch", BRANCH)
        pull = holophyte.pr.pr_status.parse_pr_url(self.URL)
        self.assertEqual(
            holophyte.pr.pr_status.pr_state(self.project, pull).threads, ()
        )
        replies = [data["body"] for kind, data in self.api_calls()
                   if kind == "conversation"]
        self.assertEqual(len(replies), 1)
        self.assertIn("\n\n---- Comment by ", replies[0])
        self.assertIn("Only listed maintainers may instruct the factory here",
                      replies[0])
        state["data"]["repository"]["pullRequest"]["comments"]["nodes"].append(
            self.comment(2, ("factory", "User"), replies[0]))
        self.serve(state)
        self.assertEqual(
            holophyte.pr.pr_status.pr_state(self.project, pull).threads, ()
        )
        self.assertEqual(sum(kind == "conversation" for kind, _ in self.api_calls()), 1)
