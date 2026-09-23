"""Mention identity boundaries exercised through the fake GitHub."""
from fake_agent import APPROVE, Commit, Idle
from loop_fixture import BRANCH

import holophyte.pr_status
from holophyte import babysitter, thread_mentions
from holophyte.config_tables import merge_config


class MentionAccountCases:
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


    def test_listed_login_is_case_insensitive(self):
        self.mention_accounts = 'mention_accounts = ["OpErAtOr"]\n'
        self.mentioned_thread_is_fixed(("reviewer", "User"))

    def test_unlisted_mentions_keep_kind_and_refuse_once(self):
        self.configure('[merge]\nmode = "pr"\nmention_accounts = ["operator"]\n')
        request = "@holophyte fix: change tokens"
        threads = [("src/app.py", 30, (login, kind), request)
                   for login, kind in (("stranger", "User"), ("robot", "Bot"))]
        self.fake_route(states=[self.pr_state(threads)])
        self.git("branch", BRANCH)
        pull = holophyte.pr_status.parse_pr_url(self.URL)
        state = babysitter._settled_state(self.project, None, None, 1, pull)
        merge = merge_config(self.project)
        classified = [thread_mentions.classify(t, merge.mention_handle,
                                              merge.mention_accounts)
                      for t in state.threads]
        self.assertEqual([t.author_kind for t in classified], ["user", "bot"])
        self.assertTrue(all(t.classification != "MENTIONED" for t in classified))
        replies = [data["body"] for kind, data in self.api_calls() if kind == "reply"]
        self.assertEqual(len(replies), 2)
        for body in replies:
            self.assertTrue(body.startswith("---- Comment by "))
            self.assertIn("Only listed maintainers may instruct the factory here", body)
        # A new refused mention after the factory reply must not elicit another.
        repeated = [(*thread, ((("factory", "User"), body),
                               ((thread[2][0], thread[2][1]), request)))
                    for thread, body in zip(threads, replies)]
        self.serve(self.pr_state(repeated))
        babysitter._settled_state(self.project, None, None, 1, pull)
        self.assertEqual(sum(kind == "reply" for kind, _ in self.api_calls()), 2)

    def test_unlisted_conversation_mention_refuses_once(self):
        self.configure('[merge]\nmode = "pr"\nmention_accounts = ["operator"]\n')
        state = self.conversation_state(("stranger", "User"),
                                        "@holophyte fix: change tokens")
        self.fake_route(states=[state])
        self.git("branch", BRANCH)
        pull = holophyte.pr_status.parse_pr_url(self.URL)
        self.assertEqual(holophyte.pr_status.pr_state(self.project, pull).threads, ())
        replies = [data["body"] for kind, data in self.api_calls()
                   if kind == "conversation"]
        self.assertEqual(len(replies), 1)
        self.assertIn("\n\n---- Comment by ", replies[0])
        self.assertIn("Only listed maintainers may instruct the factory here",
                      replies[0])
        state["data"]["repository"]["pullRequest"]["comments"]["nodes"].append(
            self.comment(2, ("factory", "User"), replies[0]))
        self.serve(state)
        self.assertEqual(holophyte.pr_status.pr_state(self.project, pull).threads, ())
        self.assertEqual(sum(kind == "conversation" for kind, _ in self.api_calls()), 1)
