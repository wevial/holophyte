"""KO-475 cases inherited by babysitter, config and run-detail test modules."""
from fake_agent import APPROVE, Commit, Idle, Reply

import store


class BotThreadCases:
    def test_storeless_advisory_thread_is_replied_and_resolved(self):
        from loop_fixture import BRANCH

        from holophyte import babysitter, pr_status

        self.configure('[merge]\nmode = "pr"\nbot_threads = "advisory"\n')
        person = ("src/app.py", 30, ("maintainer", "User"), "Fix human finding")
        self.fake_route(states=[self.pr_state([self.DEFECT, person])])
        self.git("branch", BRANCH)
        pull = pr_status.parse_pr_url(self.URL)
        state = babysitter._settled_state(self.tgt, None, None, 1, pull)
        self.assertEqual([thread.body for thread in state.threads], [person[3]])
        calls = self.api_calls()
        replies = [data for kind, data in calls if kind == "reply"]
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0]["body"].startswith("---- Comment by "))
        self.assertIn("Noted as advisory for the maintainer; "
                      "not acted on by the factory.", replies[0]["body"])
        self.assertIn(("resolve", {"thread": "PRRT_1"}), calls)

    def test_bot_thread_policy_routes_only_actionable_findings(self):
        self.configure('[merge]\nmode = "pr"\nhuman_threads = "act"\n'
                       + getattr(self, "bot_policy", 'bot_threads = "advisory"\n'))
        bot = (*self.DEFECT[:2], getattr(self, "bot_author", "review-bot"),
               self.DEFECT[3], getattr(self, "bot_replies", ()))
        person = ("src/app.py", 30, ("maintainer", "User"), "Fix the human finding")
        advisory = getattr(self, "expect_advisory", True)
        self.fake_route(states=[self.pr_state([bot, person])])
        verdict = "THREAD 1: ADDRESS -- fix finding"
        if not advisory:
            verdict += "\nTHREAD 2: ADDRESS -- fix human finding"
        fake, _ = self.loop(Commit("initial work"), APPROVE, Idle(""),
                            Reply(verdict), Commit("fix actionable findings"),
                            provider=self.provider())
        goal = fake.turns[4].goal
        self.assertIn(person[3], goal)
        self.assertEqual(self.DEFECT[3] in goal, not advisory)
        calls = self.api_calls()
        replies = [data for kind, data in calls if kind == "reply"]
        events = self.read("SELECT summary FROM runEvents WHERE kind = 'bot_finding'")
        if advisory:
            self.assertTrue(replies[0]["body"].startswith("---- Comment by "))
            self.assertIn("Noted as advisory for the maintainer; "
                          "not acted on by the factory.",
                          replies[0]["body"])
            self.assertIn(("resolve", {"thread": "PRRT_1"}), calls)
            self.assertEqual(len(events), 1)
            self.assertIn(self.URL + "#discussion_r1", events[0][0])
            self.assertIn(self.DEFECT[3], events[0][0])
        else:
            self.assertEqual(events, [])

    def test_explicit_act_keeps_bot_findings_actionable(self):
        self.bot_policy = 'bot_threads = "act"\n'
        self.expect_advisory = False
        self.test_bot_thread_policy_routes_only_actionable_findings()

    def test_default_keeps_bot_findings_actionable(self):
        self.bot_policy = ""
        self.expect_advisory = False
        self.test_bot_thread_policy_routes_only_actionable_findings()

    def test_configured_login_is_advisory(self):
        self.bot_policy = 'bot_threads = "advisory"\nbot_logins = ["service"]\n'
        self.bot_author = ("service", "User")
        self.test_bot_thread_policy_routes_only_actionable_findings()

    def test_human_reply_escalates_advisory_bot_thread(self):
        self.bot_replies = ((("maintainer", "User"), "Please fix this"),)
        self.expect_advisory = False
        self.test_bot_thread_policy_routes_only_actionable_findings()

    def test_only_advisory_threads_merge_without_a_fix_round(self):
        self.configure('[merge]\nmode = "pr"\nbot_threads = "advisory"\n'
                       'pr_rounds = 1\n')
        self.fake_route(states=[self.pr_state([self.DEFECT])])
        fake, _ = self.loop(Commit("initial work"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertNotIn("adjudicate", fake.roles)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])


class BotConfigCases:
    def test_mention_accounts_validation_and_open_startup_notice(self):
        import contextlib
        import io

        from holophyte.config import check_document, merge_config
        from holophyte.startup import banner

        for value in ('"operator"', '[1]', 'false'):
            self.locate(f'[merge]\nmention_accounts = {value}\n')
            with self.assertRaisesRegex(
                    SystemExit, "mention_accounts.*list of strings"):
                check_document(self.tgt)
        for config, open_notice in (("", False),
                                   ('human_threads = "act"', True),
                                   ('human_threads = "act"\n'
                                    'mention_accounts = []', True),
                                   ('human_threads = "act"\n'
                                    'mention_accounts = ["Operator"]', False)):
            self.locate("[merge]\n" + config + "\n")
            check_document(self.tgt)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                banner(self.tgt)
            self.assertEqual(
                output.getvalue().count("mentions are open to any account"),
                int(open_notice))
        self.assertEqual(merge_config(self.tgt).mention_accounts, ("Operator",))

    def test_bot_thread_config_validation(self):
        from holophyte.config_tables import merge_config
        self.locate('[merge]\nbot_threads = "sometimes"\n')
        with self.assertRaisesRegex(SystemExit, '"act" or "advisory"'):
            merge_config(self.tgt)
        self.locate('[merge]\nbot_logins = [42]\n')
        with self.assertRaisesRegex(SystemExit, 'bot_logins must be a list of strings'):
            merge_config(self.tgt)


class BotFindingCases:
    def test_bot_findings_are_advisory_on_run_detail(self):
        self.seed_reviewed()
        conn = store.open(str(self.db))
        try:
            store.record_event(conn, self.run, "bot_finding",
                               "https://example.test/thread: Consider a rename")
        finally:
            conn.close()
        self.start()
        code, _, body = self.request("GET", f"/runs/{self.run}")
        self.assertEqual(code, 200)
        self.assertEqual(body["findings"], [{
            "tone": "advisory",
            "message": "https://example.test/thread: Consider a rename",
        }])
